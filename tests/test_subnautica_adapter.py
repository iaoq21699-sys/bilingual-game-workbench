"""Patch lifecycle checks in disposable fake game trees; no installed game access."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bilingual.adapters import subnautica


class SubnauticaPatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-sn-check-")
        self.root = Path(self.temp.name).resolve()
        self.mod = self.root / "BilingualMod"
        self.paks = self.root / "Subnautica2/Content/Paks"
        self.mod.mkdir()
        self.paks.mkdir(parents=True)
        hashes = {}
        for name in subnautica.PACKAGE_NAMES:
            value = ("fixture-" + name).encode()
            (self.mod / name).write_bytes(value)
            hashes[name] = hashlib.sha256(value).hexdigest()
        (self.mod / "build-report.json").write_text(json.dumps({"packages": hashes}), encoding="utf-8")
        self.write_version(subnautica.BUILD)
        running = patch.object(subnautica, "_running", return_value=False)
        self.running = running.start()
        self.addCleanup(running.stop)

    def tearDown(self):
        # Explicitly constrain recursive cleanup to our named temporary directory.
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith("bilingual-sn-check-"))
        self.temp.cleanup()

    def write_version(self, version):
        (self.root / "version.json").write_text(json.dumps({"changelist": version}), encoding="utf-8")

    def assert_installed(self):
        for name in subnautica.PACKAGE_NAMES:
            self.assertEqual((self.paks / name).read_bytes(), (self.mod / name).read_bytes())

    def assert_uninstalled(self):
        self.assertEqual(list(self.paks.iterdir()), [])

    def test_enable_disable_preserves_earlier_backups(self):
        old_disabled = self.mod / "disabled"
        old_disabled.mkdir()
        existing_backup = old_disabled / subnautica.PACKAGE_NAMES[0]
        existing_backup.write_bytes(b"preserve earlier backup")

        subnautica.action(self.root, "enable")
        self.assert_installed()
        subnautica.action(self.root, "disable")
        self.assert_uninstalled()
        self.assertEqual(existing_backup.read_bytes(), b"preserve earlier backup")
        backups = list(self.mod.glob("disabled-workbench-*"))
        self.assertEqual(len(backups), 1)
        for name in subnautica.PACKAGE_NAMES:
            self.assertEqual((backups[0] / name).read_bytes(), (self.mod / name).read_bytes())

    def test_version_mismatch_rejects_enable_without_writes(self):
        self.write_version(1)
        with self.assertRaises(RuntimeError):
            subnautica.action(self.root, "enable")
        self.assert_uninstalled()

    def test_foreign_target_is_preserved_and_blocks_all_copies(self):
        foreign = self.paks / subnautica.PACKAGE_NAMES[1]
        foreign.write_bytes(b"foreign patch")
        with self.assertRaises(ValueError):
            subnautica.action(self.root, "enable")
        self.assertEqual(foreign.read_bytes(), b"foreign patch")
        self.assertEqual(list(self.paks.iterdir()), [foreign])

    def test_enable_copy_failure_rolls_back_new_files(self):
        original = subnautica.shutil.copyfileobj
        calls = 0

        def fail_second_copy(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected copy failure")
            return original(source, target)

        with patch.object(subnautica.shutil, "copyfileobj", fail_second_copy):
            with self.assertRaises(OSError):
                subnautica.action(self.root, "enable")
        self.assert_uninstalled()

    def test_disable_rename_failure_restores_installed_files(self):
        subnautica.action(self.root, "enable")
        original = subnautica.os.rename
        calls = 0

        def fail_second_rename(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected rename failure")
            return original(source, target)

        with patch.object(subnautica.os, "rename", fail_second_rename):
            with self.assertRaises(OSError):
                subnautica.action(self.root, "disable")
        self.assert_installed()
        self.assertEqual(list(self.mod.glob("disabled-workbench-*")), [])

    def test_running_game_blocks_enable_without_writes(self):
        self.running.return_value = True
        with self.assertRaises(RuntimeError):
            subnautica.action(self.root, "enable")
        self.assert_uninstalled()


if __name__ == "__main__":
    unittest.main()
