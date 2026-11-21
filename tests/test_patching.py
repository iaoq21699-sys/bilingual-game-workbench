"""Owned-file install/restore transactions in temporary fake game directories."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bilingual import patching


class PatchTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-patching-check-")
        self.scratch = Path(self.temp.name).resolve()
        self.root = self.scratch / "game"
        self.root.mkdir()
        self.staged = self.scratch / "staged"
        self.staged.mkdir()
        self.files = []
        for index in range(3):
            source = self.staged / f"patch{index}.pak"
            source.write_bytes(f"bilingual-{index}".encode())
            self.files.append({"source": str(source), "target": f"Content/Paks/patch{index}.pak"})
        self.manager = patching.PatchSet(self.root, "fake-game")

    def tearDown(self):
        self.assertTrue(self.scratch.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.scratch.name.startswith("bilingual-patching-check-"))
        self.temp.cleanup()

    def assert_installed(self):
        for row in self.files:
            self.assertEqual((self.root / row["target"]).read_bytes(), Path(row["source"]).read_bytes())

    def assert_removed(self):
        for row in self.files:
            self.assertFalse((self.root / row["target"]).exists())

    def test_new_files_enable_disable_preserves_unrelated_game_data(self):
        unrelated = self.root / "save.dat"
        unrelated.write_bytes(b"player-save")
        self.manager.enable(self.files)
        self.assert_installed()
        self.manager.disable()
        self.assert_removed()
        self.assertEqual(unrelated.read_bytes(), b"player-save")
        self.assertEqual(self.manager.state()["phase"], "disabled")

    def test_fingerprinted_original_is_backed_up_and_restored(self):
        row = self.files[0]
        original = self.root / row["target"]
        original.parent.mkdir(parents=True)
        original.write_bytes(b"original-language-file")
        fingerprint = {"path": row["target"], "sha256": patching.digest(original)}
        self.manager.enable([row], [fingerprint])
        self.assertEqual(original.read_bytes(), Path(row["source"]).read_bytes())
        backup = patching.inside(self.manager.home, self.manager.state()["files"][0]["backup"])
        self.assertEqual(backup.read_bytes(), b"original-language-file")
        self.manager.disable()
        self.assertEqual(original.read_bytes(), b"original-language-file")
        self.assertEqual(backup.read_bytes(), b"original-language-file")

    def test_unowned_existing_target_blocks_install(self):
        target = self.root / self.files[1]["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"another-mod")
        with self.assertRaises(ValueError):
            self.manager.enable(self.files)
        self.assertEqual(target.read_bytes(), b"another-mod")
        self.assertFalse((self.root / self.files[0]["target"]).exists())

    def test_second_install_copy_failure_rolls_back_first_file(self):
        original_copy = patching._atomic_copy
        calls = 0

        def fail_second_copy(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second-copy failure")
            return original_copy(source, target)

        with patch.object(patching, "_atomic_copy", fail_second_copy):
            with self.assertRaises(OSError):
                self.manager.enable(self.files)
        self.assert_removed()
        self.assertEqual(self.manager.state()["phase"], "disabled")

    def test_second_restore_failure_returns_to_enabled_state(self):
        self.manager.enable(self.files)
        original_restore = self.manager._restore_original
        calls = 0

        def fail_second_restore(row):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second-restore failure")
            return original_restore(row)

        with patch.object(self.manager, "_restore_original", fail_second_restore):
            with self.assertRaises(OSError):
                self.manager.disable()
        self.assert_installed()
        self.assertEqual(self.manager.state()["phase"], "enabled")

    def test_modified_patch_blocks_restore_without_overwriting(self):
        self.manager.enable(self.files)
        changed = self.root / self.files[1]["target"]
        changed.write_bytes(b"external-change")
        with self.assertRaises(ValueError):
            self.manager.disable()
        self.assertEqual(changed.read_bytes(), b"external-change")
        self.assertEqual((self.root / self.files[0]["target"]).read_bytes(), Path(self.files[0]["source"]).read_bytes())
        self.assertEqual(self.manager.state()["phase"], "enabled")

    def test_updated_source_blocks_install(self):
        source = self.root / "resource.pak"
        source.write_bytes(b"game-original")
        fingerprint = {"path": "resource.pak", "sha256": patching.digest(source)}
        source.write_bytes(b"game-update")
        with self.assertRaises(ValueError):
            self.manager.enable(self.files, [fingerprint])
        self.assert_removed()

    def test_parent_escape_is_rejected_before_writing_outside_game(self):
        row = dict(self.files[0], target="../outside.pak")
        with self.assertRaises(ValueError):
            self.manager.enable([row])
        self.assertFalse((self.scratch / "outside.pak").exists())


if __name__ == "__main__":
    unittest.main()
