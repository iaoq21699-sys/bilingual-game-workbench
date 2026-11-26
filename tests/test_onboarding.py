"""Read-only onboarding diagnostics with temporary fictional game directories."""
from pathlib import Path
import tempfile
import unittest

from bilingual.core import Service
from bilingual.engines.pck import write
from bilingual.onboarding import describe


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        base = Path(fixture.name)
        self.game = base / "Fictional Game"
        self.game.mkdir()
        self.service = Service(base / "workbench")

    def test_missing_exe_produces_directory_guidance_without_writes(self):
        report = self.service.inspect_game(self.game)
        self.assertEqual(report["diagnostic"], "missing_exe")
        self.assertIn("Steam", describe(report)[1])
        with self.assertRaises(ValueError):
            self.service.connect_game(report)
        self.assertEqual(list(self.game.iterdir()), [])
        self.assertEqual(self.service.games(), [])

    def test_unrecognized_exe_does_not_report_absent_unreal_tool(self):
        (self.game / "Game.exe").write_bytes(b"Fixture only")
        report = self.service.inspect_game(self.game)
        self.assertEqual(report["diagnostic"], "unsupported")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["executables"], ["Game.exe"])

    def test_bad_explicit_profile_fails_closed(self):
        exe = self.game / "Game.exe"
        exe.touch()
        configuration = self.game / "bilingual-game.json"
        configuration.write_text('{"engine":"unknown"}', encoding="utf8")
        report = self.service.inspect_game(self.game)
        self.assertEqual(report["diagnostic"], "invalid_profile")
        self.assertEqual(describe(report)[2], "接入配置")
        self.assertEqual(configuration.read_text(encoding="utf8"), '{"engine":"unknown"}')

    def test_pck_with_two_languages_but_unknown_loader_is_not_installable(self):
        (self.game / "Game.exe").touch()
        pack = self.game / "Fiction.pck"
        write(pack, {"locales/en/menu.json": b'{"menu":"Go"}',
                     "locales/zh/menu.json": b'{"menu":"Start"}'})
        original = pack.read_bytes()
        report = self.service.inspect_game(self.game)
        self.assertEqual(report["diagnostic"], "resource_issue")
        self.assertIn("加载方式", str(report["issues"]))
        self.assertEqual(pack.read_bytes(), original)

    def test_garbage_asar_has_context_and_can_fall_back_to_valid_loose_json(self):
        (self.game / "Game.exe").touch()
        (self.game / "resources").mkdir()
        (self.game / "resources/app.asar").write_bytes(b"not an archive")
        report = self.service.inspect_game(self.game)
        self.assertEqual(report["diagnostic"], "resource_issue")
        self.assertEqual(describe(report)[2], "Electron · ASAR")
        (self.game / "en.json").write_text('{"hi":"Hi"}', encoding="utf8")
        (self.game / "zh.json").write_text('{"hi":"你好"}', encoding="utf8")
        new_report = self.service.inspect_game(self.game)
        self.assertTrue(new_report["connectable"])
        self.assertEqual(new_report["engine"], "loose_json")
