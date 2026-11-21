"""Disposable ASAR fixtures; no installed game's contents or processes are used."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from bilingual.core import Service
from bilingual.engines import electron
from bilingual.engines.asar import Archive
from bilingual.engines.jsdata import OPAQUE, parse_data_module
from bilingual.profiles import Profiles


def make_asar(path, resources):
    files, contents = {}, bytearray()
    for name, value in resources.items():
        data = value if isinstance(value, bytes) else value.encode("utf-8")
        tree = files
        parts = name.split("/")
        for part in parts[:-1]:
            tree = tree.setdefault(part, {"files": {}})["files"]
        tree[parts[-1]] = {"size": len(data), "offset": str(len(contents))}
        contents.extend(data)
    raw = json.dumps({"files": files}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padded = (len(raw)+3) & ~3
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<IIII", 4, 8+padded, 4+padded, len(raw)) +
                     raw + bytes(padded-len(raw)) + contents)


class ElectronTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-electron-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "ExampleGame"
        self.root.mkdir()
        (self.root / "ExampleGame.exe").write_bytes(b"fixture, never launched")
        (self.root / "LICENSE.electron.txt").write_text("fixture", encoding="utf-8")
        self.archive = self.root / "resources" / "app.asar"
        self.service = Service(project=self.base / "Workbench")
        self.profiles = Profiles(self.service)

    @staticmethod
    def json(value):
        return json.dumps(value, ensure_ascii=False)

    def simple(self):
        make_asar(self.archive, {"package.json": self.json({"name": "fictional-example", "version": "1"}),
                                "locales/en.json": self.json({"hello": "Hello", "dialogue": {"short": "Good morning"}, "locale": "en"}),
                                "locales/zh-CN.json": self.json({"hello": "你好", "dialogue": {"short": "早安"}, "locale": "zh-CN"}),
                                "main.js": b"fixture implementation, must never be read"})

    def test_asar_json_profile_is_corpus_only_and_does_not_write_game(self):
        self.simple()
        before = self.archive.read_bytes()
        manifest = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        profile = self.profiles.inspect(self.root)
        self.assertEqual(profile["engine"], "electron")
        self.assertTrue(profile["corpus_only"])
        self.assertEqual(len(profile["pairs"]), 1)
        with patch("bilingual.profiles.PatchSet") as patch_set, patch.object(self.profiles, "create_launcher") as launcher:
            result = self.profiles.connect(profile)
            patch_set.assert_not_called()
            launcher.assert_not_called()
        self.assertEqual((result["count"], result["paired"]), (2, 2))
        self.assertTrue(result["corpus_only"])
        gid = result["id"]
        game = self.service.game_info(gid)
        self.assertTrue(game["supports_native"])  # ordinary launch/session tracking
        self.assertFalse(game["supports_patch"])
        self.assertFalse(game["supports_launcher"])
        self.assertTrue(game["corpus_only"])
        self.assertEqual(self.service.search("早安", gid)["total"], 1)
        self.assertIn("原版显示", self.profiles.action(gid, "status"))
        with self.assertRaises(ValueError):
            self.profiles.action(gid, "enable")
        with self.assertRaises(ValueError):
            self.profiles.create_launcher(gid)
        with patch.object(self.profiles, "_running", return_value=False), patch("bilingual.profiles.subprocess.Popen") as launch:
            self.profiles.action(gid, "launch")
            launch.assert_called_once_with([str(self.root / "ExampleGame.exe")], cwd=str(self.root))
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(manifest, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")))

    def test_recognizes_unsupported_electron_without_claiming_a_pair(self):
        make_asar(self.archive, {"package.json": self.json({"name": "unknown"}), "dist/game.js": "const ignored = 1;"})
        profile = self.profiles.inspect(self.root)
        self.assertEqual(profile["engine"], "electron")
        self.assertTrue(profile["recognized"])
        self.assertFalse(profile["connectable"])
        with self.assertRaises(ValueError):
            self.profiles.connect(profile)
        self.assertEqual(self.service.games(), [])

    def test_electron_panel_preflight_blocks_unverified_install_even_with_directx_selection(self):
        from bilingual import native_overlay
        self.simple()
        gid = self.profiles.connect(self.profiles.inspect(self.root))["id"]
        for name in ("tools/reshade-runtime/ReShade64.dll", "native/build/bin/Release/BilingualPanel.addon64"):
            path = self.service.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture, never installed")
        static = {"architecture": "x64", "apis": ["D3D11"], "imports": []}
        evidence = {"source": "fixture renderer observation", "panel_verified": False}
        with patch("bilingual.native_overlay.inspect_executable", return_value=static), \
                patch("bilingual.native_electron.inspect", return_value=evidence), \
                patch("bilingual.native_overlay.GameSession") as sessions:
            sessions.return_value.is_running.return_value = False
            report = native_overlay.inspect(self.service, gid, renderer="D3D11")
            self.assertFalse(report["ready"])
            self.assertTrue(any("Electron" in value for value in report["blockers"]))
            with self.assertRaisesRegex(ValueError, "Electron"):
                native_overlay.install(self.service, gid)
        self.assertFalse((self.root / "dxgi.dll").exists())
        self.assertFalse((self.root / ".bilingual-workbench").exists())

    @unittest.skipUnless(os.name == "nt", "Electron pipe backend uses Windows inherited handles")
    def test_local_html_panel_setup_dispatches_and_keeps_game_files_read_only(self):
        from bilingual import electron_panel, native_overlay
        make_asar(self.archive, {"package.json": self.json({"name": "example", "version": "1"}),
                                "dist/index.html": "<p>Hello there</p>",
                                "locales/en.json": self.json({"greeting": "Hello there"}),
                                "locales/zh.json": self.json({"greeting": "你好呀"})})
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        gid = self.profiles.connect(self.profiles.inspect(self.root))["id"]
        profile = self.profiles.load(gid)
        report = native_overlay.inspect(self.service, gid)
        self.assertEqual(report["backend"], "electron")
        self.assertTrue(report["ready"], report["blockers"])
        _, archive, pages, schema = electron_panel.installation(profile)
        url = archive.path.as_uri() + "/dist/index.html"
        self.assertEqual(electron_panel.page_name(url + "#main", archive.path, pages), "dist/index.html")
        self.assertIsNone(electron_panel.page_name(url.replace(".asar/", ".asar.fake/"), archive.path, pages))
        self.assertIsNone(electron_panel.page_name(url.replace("file:", "https:"), archive.path, pages))
        self.assertIsNone(electron_panel.page_name(url.replace("index.html", "settings.html"), archive.path, pages))
        self.assertIsNone(schema)
        native_overlay.install(self.service, gid)
        with patch.object(self.profiles, "_running", return_value=False), \
                patch("bilingual.electron_panel.launch", return_value="web panel") as launch:
            self.assertEqual(self.profiles.action(gid, "launch"), "web panel")
            launch.assert_called_once_with(self.service, profile)
        native_overlay.remove(self.service, gid)
        self.assertFalse(electron_panel.enabled(self.service, gid))
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")))

    def test_renderer_library_search_lookup_and_notebook_deduplication(self):
        from bilingual.electron_runtime import Library
        from bilingual.notes import Notebook
        self.simple()
        gid = self.profiles.connect(self.profiles.inspect(self.root))["id"]
        library = Library(self.service, self.profiles.load(gid))
        search = library.handle(dict(action="search", query="早安"))
        (entry,) = search["items"]
        self.assertEqual(entry["en"], "Good morning")
        filtered = library.handle(dict(action="search", category=entry["category"], offset=1))
        self.assertEqual((filtered["total"], len(filtered["items"])), (2, 1))
        self.assertIn("meaning", library.handle(dict(action="lookup", entry=entry["id"], term="morning")))
        with self.assertRaisesRegex(ValueError, "连续"):
            library.handle(dict(action="lookup", entry=entry["id"], term="missing"))
        note = dict(action="note", entry=entry["id"], term="Good morning")
        self.assertEqual(library.handle(note), {"saved": True})
        self.assertEqual(library.handle(note), {"saved": False})
        book = Notebook(self.service.data_dir)
        _, rows = book.listing(gid)
        self.assertEqual(len(rows), 1)
        row = book.get(rows[0]["id"])
        self.assertEqual((row["en"], row["zh"], row["kind"]), ("Good morning", "早安", "phrase"))

    def test_renderer_and_desktop_share_saved_appearance(self):
        from bilingual import native_overlay, themes
        from bilingual.electron_runtime import Library
        self.simple()
        gid = self.profiles.connect(self.profiles.inspect(self.root))["id"]
        profile = self.profiles.load(gid)
        library = Library(self.service, profile)
        warm = dict(themes.presets()["warm"], Opacity=0.35)
        saved = library.handle(dict(action="appearance", theme=warm, cardSide="left"))
        self.assertEqual(saved["theme"]["Opacity"], 0.35)
        initial = Library(self.service, profile).properties(Archive(self.archive), None)
        self.assertEqual((initial["theme"]["Opacity"], initial["cardSide"]), (0.35, "left"))
        with self.assertRaises(ValueError):
            library.handle(dict(action="appearance", theme=warm, cardSide="invalid"))
        native_overlay.save_preferences(self.service, gid, dict(warm, Opacity=0.95))
        updated = native_overlay.preferences(self.service, gid)
        self.assertEqual((updated["theme"]["Opacity"], updated["card_side"]), (0.95, "left"))

    def test_scene_alignment_preserves_locale_specific_slots_and_omits_ambiguous_lines(self):
        from bilingual.engines.electron_red_pearls_reading import _clues
        left = {"room_1": {"clue": "@p1: First\n\n@note: Maybe\n@p2: Last"}}
        right = {"room_1": {"clue": "@p1: 第一条\n@note: 可能\n@note: 新旁白\n\n@p2: 最后一条"}}
        rows = _clues(left, right, str.strip)["room_1"]
        self.assertEqual(rows, [[0, 0, "p1", ["First", "第一条"]], [3, 4, "p2", ["Last", "最后一条"]]])

    def test_journey_dates_use_the_calendar_and_period_instead_of_list_position(self):
        from bilingual.engines.electron_red_pearls_reading import _dated_pairs
        result = _dated_pairs(["December 17th 1941", "Evening December 17th", "Morning December 17th"],
                              ["12月17日 早", "1941年12月17日", "12月17日 晚"])
        self.assertEqual(dict(result), {"December 17th 1941": "1941年12月17日",
                                       "Evening December 17th": "12月17日 晚", "Morning December 17th": "12月17日 早"})

    def test_steam_launch_requires_matching_local_installation_manifest(self):
        root = self.base / "SteamLibrary" / "steamapps" / "common" / "SteamExample"
        root.mkdir(parents=True)
        (root / "SteamExample.exe").write_bytes(b"not executed")
        make_asar(root / "resources/app.asar", {
            "package.json": self.json({"name": "example"}), "steam_appid.txt": "12345\n",
            "locales/en.json": self.json({"label": "Example"}),
            "locales/zh.json": self.json({"label": "示例"})})
        manifest = root.parent.parent / "appmanifest_12345.acf"
        manifest.write_text('"AppState"\n{\n "appid" "12345"\n "installdir" "DifferentGame"\n}', encoding="utf-8")
        self.assertIsNone(electron.steam_appid(root))
        manifest.write_text('"AppState"\n{\n "appid" "12345"\n "installdir" "SteamExample"\n}', encoding="utf-8")
        self.assertEqual(electron.steam_appid(root), "12345")
        gid = self.profiles.connect(self.profiles.inspect(root))["id"]
        if os.name == "nt":
            with patch.object(self.profiles, "_running", return_value=False), \
                    patch("bilingual.profiles.os.startfile") as launch:
                self.assertIn("请求 Steam", self.profiles.action(gid, "launch"))
                launch.assert_called_once_with("steam://rungameid/12345")

    def test_runtime_graphics_evidence_is_not_an_active_renderer_or_panel_result(self):
        from bilingual import native_electron
        profile = {"engine": "electron", "root": str(self.root), "window_executable": "ExampleGame.exe"}
        observation = {"state": "observed", "processes": [{"pid": 12345, "ok": True,
                        "modules": ["d3d11.dll", "dxgi.dll", "libglesv2.dll"]}]}
        with patch("bilingual.native_electron._loaded_modules", return_value=observation):
            evidence = native_electron.inspect(profile, observe_runtime=True)
        self.assertIn("d3d11.dll", evidence["source"])
        self.assertIsNone(evidence["active_api"])
        self.assertFalse(evidence["panel_verified"])

    def test_arrays_and_forged_pair_paths_are_not_guessed(self):
        make_asar(self.archive, {"package.json": self.json({"name": "unknown"}),
                                "locales/en.json": self.json({"rows": [{"text": "A"}]}),
                                "locales/zh.json": self.json({"rows": [{"text": "甲"}]})})
        self.assertFalse(self.profiles.inspect(self.root)["connectable"])
        self.simple()
        profile = self.profiles.inspect(self.root)
        profile["pairs"][0]["english"] = "main.js"
        with self.assertRaises(ValueError):
            self.profiles.connect(profile)
        self.assertEqual(self.service.games(), [])

    def test_asar_index_rejects_out_of_bounds_or_parent_entry(self):
        self.simple()
        self.archive.write_bytes(self.archive.read_bytes()[:-10])
        with self.assertRaises(ValueError):
            Archive(self.archive)
        make_asar(self.archive, {"../en.json": "{}"})
        with self.assertRaises(ValueError):
            Archive(self.archive)

    def test_known_schema_pairs_ids_and_groups_ambiguous_dialogue(self):
        # Synthetic data, not text from the commercial title. The Chinese
        # records deliberately have a different order from the English ones.
        people_en = [{"id": "one", "firstName": "Alex"}, {"id": "two", "firstName": "Morgan"}]
        people_zh = [{"id": "two", "firstName": "摩根"}, {"id": "one", "firstName": "亚历克斯"}]
        line1 = "A clearly fictional English sentence for our language resource unit test. " * 3
        line2 = "Another entirely invented example sentence for a separate dialogue field. " * 3
        z1, z2 = "这是单元测试的原创文本。" * 12, "这是另一段独立的测试句子。" * 12
        dialogue_en = {"GROUP": {"title": "Example scene", "lines": [
            {"speaker": "A", "text": line1, "portrait": "same"}, {"speaker": "A", "text": line2, "portrait": "same"}]},
            "UNIQUE": {"lines": [{"speaker": "A", "text": "First unique", "portrait": "p1"},
                                 {"speaker": "B", "text": "Second unique", "portrait": "p2"}]}}
        dialogue_zh = {"GROUP": {"title": "示例场景", "lines": [
            {"speaker": "甲", "text": z1, "portrait": "same"}, {"speaker": "甲", "text": z2, "portrait": "same"}]},
            "UNIQUE": {"lines": [{"speaker": "乙", "text": "第二句", "portrait": "p2"},
                                 {"speaker": "甲", "text": "第一句", "portrait": "p1"}]}}
        def module(value):
            return "const t=" + self.json(value) + ";export{t};"
        make_asar(self.archive, {"package.json": self.json({"name": "the-red-pearls-of-borneo"}),
                                "dist/assets/data-enhash.js": module(people_en),
                                "dist/assets/data_cn-zhhash.js": module(people_zh),
                                "dist/assets/dialogue-english.js": module(dialogue_en),
                                "dist/assets/dialogue-chinese.js": module(dialogue_zh),
                                "dist/index.js": b"must not read"})
        profile = self.profiles.inspect(self.root)
        self.assertEqual(len(profile["pairs"]), 2)
        result = electron.build(self.root, profile, self.base)
        rows = result["entries"]
        pairs = {r["en"]: r["zh"] for r in rows}
        self.assertEqual(pairs["Alex"], "亚历克斯")
        self.assertEqual(pairs["Morgan"], "摩根")
        self.assertEqual(pairs["First unique"], "第一句")
        self.assertEqual(pairs["Second unique"], "第二句")
        groups = [r for r in rows if r["category"] == "剧情对白·场景段落"]
        self.assertEqual(len(groups), 1)
        self.assertIn(line1, groups[0]["en"])
        self.assertIn(z2, groups[0]["zh"])
        self.assertTrue(groups[0]["warnings"])
        self.assertEqual(result["report"]["totals"]["dialogue_lines"], 4)
        self.assertFalse(result["files"])

    def test_static_literals_skip_calls_dynamic_templates_and_unknown_keys(self):
        roots, skipped = parse_data_module(b'const r={title:"Hello",text:`Hello world`,check:()=>false,other:lookup(),dynamic:`${name}`};export{r};')
        self.assertEqual(roots[0]["title"], "Hello")
        self.assertEqual(roots[0]["text"], "Hello world")
        self.assertIs(roots[0]["check"], OPAQUE)
        self.assertIs(roots[0]["other"], OPAQUE)
        self.assertIs(roots[0]["dynamic"], OPAQUE)
        self.assertGreaterEqual(skipped, 3)
        self.assertEqual(parse_data_module(b'const r={title:"Hello",...unknown};export{r};')[0], [])


if __name__ == "__main__":
    unittest.main()
