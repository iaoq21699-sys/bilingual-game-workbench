"""PCK and overlay integrity checks, isolated from installed games."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bilingual.engines.godot import build, probe, _separator
from bilingual.engines.pck import Archive, write


class GodotOverlayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="godot-bilingual-")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_pack_round_trip_and_corruption_is_rejected(self):
        pack = self.root / "test.pck"
        resources = {"locale/菜单.json": '{"hello":"你好"}'.encode(), "plain.json": b"{}"}
        write(pack, resources)
        archive = Archive(pack)
        self.assertEqual({key: archive.read(key) for key in archive.entries}, resources)
        entry = archive.entries["plain.json"]
        with pack.open("r+b") as file:
            file.seek(entry.offset)
            file.write(b"!")
        with self.assertRaises(ValueError):
            archive.read("plain.json")
        with self.assertRaises(ValueError):
            write(self.root / "unsafe.pck", {"../escape.json": b"{}"})
        self.assertFalse((self.root / "unsafe.pck").exists())

    def test_staging_keeps_missing_and_mismatched_text_and_does_not_install(self):
        game = self.root / "game"
        game.mkdir()
        source = {"MATCH": "Hi {name}", "MISMATCH": "Gain {amount}", "ONLY_EN": "Untranslated"}
        target = {"MATCH": "你好{name}", "MISMATCH": "获得{other}", "ONLY_ZH": "仅中文"}
        pack = game / "sample.pck"
        write(pack, {"localization/eng/menu.json": json.dumps(source).encode(),
                     "localization/zhs/menu.json": json.dumps(target).encode()})
        original_hash = hashlib.sha256(pack.read_bytes()).hexdigest()
        profile = probe(game)
        self.assertEqual(profile["options"]["loader"], "unconfigured")
        with self.assertRaises(ValueError):
            build(game, profile, self.root / "unconfigured-stage")
        profile["options"].update(loader="pck-overlay", resource_prefix="", mod_directory="mods", layout="newline")
        result = build(game, profile, self.root / "stage")
        self.assertEqual(result["report"]["entry_count"], 4)
        self.assertEqual(result["report"]["bilingual"], 1)
        self.assertEqual(result["report"]["missing_language"], 2)
        self.assertEqual(result["report"]["mismatched"], 1)
        overlay = Archive(result["files"][0]["source"])
        actual = json.loads(overlay.read("localization/zhs/menu.json"))
        self.assertEqual(actual["MATCH"], "你好{name}\nHi {name}")
        self.assertEqual(actual["MISMATCH"], "获得{other}")
        self.assertEqual(actual["ONLY_ZH"], "仅中文")
        self.assertEqual(hashlib.sha256(pack.read_bytes()).hexdigest(), original_hash)
        self.assertFalse((game / "mods").exists())

    def test_layout_keeps_short_labels_inline_and_long_sentences_separate(self):
        self.assertEqual(_separator({"layout": "auto"}, "main_menu_ui", "PLAY", "Play", "开始"), " / ")
        self.assertEqual(_separator({"layout": "auto"}, "cards", "STRIKE.title", "Strike", "打击"), " / ")
        self.assertEqual(_separator({"layout": "auto"}, "main_menu_ui", "body", "A long explanation " * 5, "较长说明"), "\n")
        self.assertEqual(_separator({"layout": "inline"}, "events", "body", "Text", "文本"), " / ")
        self.assertEqual(_separator({"layout": "newline"}, "main_menu_ui", "PLAY", "Play", "开始"), "\n")


if __name__ == "__main__":
    unittest.main()
