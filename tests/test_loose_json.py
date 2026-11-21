"""Loose language-map onboarding and restoration in a disposable fake game."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bilingual.core import Service
from bilingual.engines import loose_json
from bilingual.profiles import Profiles


class LooseJsonSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-json-check-")
        self.scratch = Path(self.temp.name).resolve()
        self.root = self.scratch / "FakeGame"
        self.languages = self.root / "Languages"
        self.languages.mkdir(parents=True)
        (self.root / "FakeGame.exe").write_bytes(b"never executed")
        self.english = self.languages / "en.json"
        self.chinese = self.languages / "zh-CN.json"
        self.write(self.english, {"locale": "en", "dialogue": {"greeting": "Hello {Name}", "unsafe": "Use {Count}"},
                                  "onlyEnglish": "English only", "enabled": True, "versionNumber": 7})
        self.write(self.chinese, {"locale": "zh-CN", "dialogue": {"greeting": "你好 {Name}", "unsafe": "使用{Other}"},
                                  "onlyChinese": "仅中文", "enabled": True, "versionNumber": 7})
        self.original_en = self.english.read_bytes()
        self.original_zh = self.chinese.read_bytes()
        self.project = self.scratch / "Application"
        self.project.mkdir()
        self.service = Service(project=self.project, data_dir=self.project / "data")
        self.manager = Profiles(self.service)
        running = patch.object(self.manager, "_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)
        launcher = patch.object(self.manager, "create_launcher", return_value="fixture-launcher")
        launcher.start()
        self.addCleanup(launcher.stop)

    def tearDown(self):
        self.assertTrue(self.scratch.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.scratch.name.startswith("bilingual-json-check-"))
        self.temp.cleanup()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_profiles_route_generates_installs_and_restores_exact_original(self):
        profile = self.manager.inspect(self.root)
        self.assertEqual(profile["engine"], "loose_json")
        profile["options"] = {"order": "en-zh", "layout": "newline"}
        result = self.manager.connect(profile)
        installed = json.loads(self.chinese.read_text(encoding="utf-8"))
        self.assertEqual(installed["dialogue"]["greeting"], "Hello {Name}\n你好 {Name}")
        self.assertEqual(installed["dialogue"]["unsafe"], "使用{Other}")
        self.assertEqual(installed["locale"], "zh-CN")
        self.assertIs(installed["enabled"], True)
        self.assertEqual(installed["versionNumber"], 7)
        self.assertNotIn("onlyEnglish", installed)
        self.assertEqual(installed["onlyChinese"], "仅中文")
        self.assertEqual(self.english.read_bytes(), self.original_en)
        self.manager.action(result["id"], "disable")
        self.assertEqual(self.chinese.read_bytes(), self.original_zh)
        self.assertEqual(self.english.read_bytes(), self.original_en)

    def test_array_resource_is_rejected_without_game_changes(self):
        self.write(self.english, {"dialogues": [{"id": "one", "text": "Hello"}]})
        before = self.english.read_bytes()
        with self.assertRaises(ValueError):
            self.manager.connect(self.manager.inspect(self.root))
        self.assertEqual(self.english.read_bytes(), before)
        self.assertEqual(self.chinese.read_bytes(), self.original_zh)
        self.assertFalse((self.root / ".bilingual-workbench/state.json").exists())

    def test_pair_path_escape_is_rejected_before_install(self):
        profile = self.manager.inspect(self.root)
        profile["pairs"][0]["target"] = "../outside.json"
        with self.assertRaises(ValueError):
            self.manager.connect(profile)
        self.assertFalse((self.scratch / "outside.json").exists())
        self.assertEqual(self.chinese.read_bytes(), self.original_zh)

    def test_source_update_during_generation_rejects_stale_patch(self):
        profile = loose_json.probe(self.root)
        stage = self.scratch / "stage"
        stage.mkdir()
        original_load = loose_json.load_json
        updated = {"locale": "zh-CN", "dialogue": {"greeting": "更新后的文本"}}

        def update_after_read(path):
            data = original_load(path)
            if Path(path) == self.chinese:
                self.write(self.chinese, updated)
            return data

        with patch.object(loose_json, "load_json", update_after_read):
            with self.assertRaisesRegex(ValueError, "生成期间"):
                loose_json.build(self.root, profile, stage)
        self.assertEqual(json.loads(self.chinese.read_text(encoding="utf-8")), updated)
        self.assertFalse((self.root / ".bilingual-workbench/state.json").exists())


if __name__ == "__main__":
    unittest.main()
