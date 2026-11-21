"""Unreal format and staging checks using a tiny fake game, never installed games."""
import json
from pathlib import Path
import tempfile
import unittest

from bilingual.engines import unreal


def resource(rows, version=3):
    return {"version": version, "entries": [
        {"ns": "ST_Dialogue", "key": key, "nh": 123 if version >= 2 else None,
         "kh": index + 100 if version >= 2 else None, "hash": index + 800, "text": text}
        for index, (key, text) in enumerate(rows)]}


class UnrealFormatTests(unittest.TestCase):
    def test_locres_roundtrip_retains_unicode_identity_and_hashes(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                data = resource([("one", "深海 🌊 {Name}"), ("two", "深海 🌊 {Name}"), ("three", "")], version)
                self.assertEqual(unreal._read_locres(unreal._write_locres(data)), data)

    def test_unsafe_parameters_preserve_original_translation(self):
        combined, issue = unreal._combine("Use {Count}", "使用{Other}", {})
        self.assertEqual(combined, "使用{Other}")
        self.assertEqual(issue, "placeholder_mismatch")
        combined, issue = unreal._combine("<b>Hello {Name}</>", "<b>你好 {Name}</>", {})
        self.assertEqual(combined, "<b>你好 {Name}</> / <b>Hello {Name}</>")
        self.assertIsNone(issue)
        self.assertEqual(unreal._combine("{Count}|plural(one=item,other=items)", "{Count}个物品", {})[1], "complex_format_preserved")


class UnrealStagingTests(unittest.TestCase):
    def setUp(self):
        self.tools = Path(__file__).resolve().parents[1] / "tools"
        if not (self.tools / "repak.exe").is_file():
            self.skipTest("Local repak fixture tool is unavailable")
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-ue-check-")
        self.scratch = Path(self.temp.name).resolve()
        self.root = self.scratch / "FakeGame"
        self.content = self.scratch / "input"
        self.paks = self.root / "Fiction/Content/Paks"
        self.paks.mkdir(parents=True)
        english = resource([("paired", "<b>Hello {Name}</>"), ("unsafe", "Use {Count}"), ("only-en", "English only")])
        chinese = resource([("paired", "<b>你好 {Name}</>"), ("unsafe", "使用{Other}"), ("only-zh", "仅中文")])
        for locale, data in (("en-US", english), ("zh-CN", chinese)):
            target = self.content / "Fiction/Content/Localization/Game" / locale / "Game.locres"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(unreal._write_locres(data))
        config = self.content / "Fiction/Config/DefaultGame.ini"
        config.parent.mkdir(parents=True)
        config.write_text('ProjectName=Fictional Game\nProjectVersion=1.0\n', encoding="utf-8")
        (self.root / "FictionalGame.exe").write_bytes(b"not executed")
        self.archive = self.paks / "pakchunk0-Windows.pak"
        unreal._run(unreal._tool(self.tools, "repak"), "pack", self.content, self.archive, "--version", "V11")
        self.original = self.archive.read_bytes()

    def tearDown(self):
        if not hasattr(self, "temp"):
            return
        self.assertTrue(self.scratch.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.scratch.name.startswith("bilingual-ue-check-"))
        self.temp.cleanup()

    def test_probe_and_build_keep_both_unpaired_languages_and_do_not_install(self):
        profile = unreal.probe(self.root, self.tools)
        self.assertEqual(profile["title"], "Fictional Game")
        self.assertEqual((profile["source_locale"], profile["target_locale"]), ("en-US", "zh-CN"))
        self.assertEqual(profile["executables"], ["FictionalGame.exe"])
        result = unreal.build(self.root, profile, self.scratch / "output", self.tools)
        records = {entry["key"].split("::")[-1]: entry for entry in result["entries"]}
        self.assertEqual(set(records), {"paired", "unsafe", "only-en", "only-zh"})
        self.assertEqual(records["only-en"]["zh"], "")
        self.assertEqual(records["only-zh"]["en"], "")
        self.assertEqual(result["report"]["counts"]["placeholder_mismatch"], 1)
        self.assertTrue(result["report"]["roundtrip_verified"])
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertEqual(list(self.paks.iterdir()), [self.archive])
        self.assertEqual(len(result["files"]), 1)

    def test_build_refuses_to_write_staging_inside_game(self):
        profile = unreal.probe(self.root, self.tools)
        with self.assertRaises(ValueError):
            unreal.build(self.root, profile, self.root / "staging", self.tools)
        self.assertFalse((self.root / "staging").exists())

    def test_iostore_generation_retains_nonasset_locres_pak(self):
        if not (self.tools / "retoc.exe").is_file():
            self.skipTest("Local retoc fixture tool is unavailable")
        profile = unreal.probe(self.root, self.tools)
        profile["options"].update(needs_iostore=True, ue_version="UE5_4")
        result = unreal.build(self.root, profile, self.scratch / "output", self.tools)
        self.assertEqual({Path(f["source"]).suffix for f in result["files"]}, {".pak", ".utoc", ".ucas"})
        self.assertTrue(result["report"]["roundtrip_verified"])
        self.assertEqual(list(self.paks.iterdir()), [self.archive])


if __name__ == "__main__":
    unittest.main()
