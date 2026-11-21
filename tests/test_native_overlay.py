"""Owned native-panel files only; fixtures never load a DLL or touch a real game."""
from contextlib import closing
import json
import sqlite3
import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bilingual import native_overlay


def game_image(machine=0x8664):
    value = bytearray(1024)
    value[:2] = b"MZ"
    struct.pack_into("<I", value, 60, 128)
    value[128:132] = b"PE\0\0"
    struct.pack_into("<HH", value, 132, machine, 1)
    struct.pack_into("<H", value, 148, 240)
    struct.pack_into("<H", value, 152, 0x20b)
    struct.pack_into("<II", value, 152 + 120, 0x1000, 40)
    struct.pack_into("<IIII", value, 392 + 8, 512, 0x1000, 512, 512)
    struct.pack_into("<I", value, 512 + 12, 0x1040)
    value[576:586] = b"d3d11.dll\0"
    return bytes(value)


class NativeOverlayInstallTests(unittest.TestCase):
    game_id = "game-native-test"
    managed = ("dxgi.dll", "BilingualPanel.addon64", "BilingualPanel.ini", "ReShade.ini")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-native-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "workbench"
        self.game = self.root / "game"
        self.relative_executable = "Sandfall/Binaries/Win64/SandFall-Win64-Shipping.exe"
        self.executable = self.game / self.relative_executable
        self.folder = self.executable.parent
        self.folder.mkdir(parents=True)
        self.executable.write_bytes(game_image())
        self.unrelated = self.folder / "user-file.txt"
        self.unrelated.write_bytes(b"unrelated user content")
        self.profile = dict(id=self.game_id, root=str(self.game), engine="unreal",
                            window_executable=self.relative_executable, title="33 号远征队测试")
        profiles = Mock()
        profiles.load.return_value = self.profile
        data = self.project / "data"
        data.mkdir(parents=True)
        self.database = data / "corpus.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE entries(game_id TEXT)")
            db.execute("INSERT INTO entries VALUES(?)", (self.game_id,))
        self.service = SimpleNamespace(project=self.project, data_dir=data,
                                       database=self.database, _profiles=lambda: profiles,
                                       _db=lambda: closing(sqlite3.connect(self.database)),
                                       session_info=lambda _: {"window_executable": str(Path(self.profile['root']) / self.profile['window_executable'])})
        game_patch = patch("bilingual.native_overlay._game", side_effect=lambda *_: self.profile)
        game_patch.start()
        self.addCleanup(game_patch.stop)
        self.runtime = self.project / "tools/reshade-runtime/ReShade64.dll"
        self.addon = self.project / "native/build/bin/Release/BilingualPanel.addon64"
        for path, content in ((self.runtime, b"fixture-runtime-not-a-DLL"),
                              (self.addon, b"fixture-addon-not-a-DLL")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.marker = data / "native_profiles" / (self.game_id + ".json")
        session_patch = patch("bilingual.native_overlay.GameSession")
        self.sessions = session_patch.start()
        self.addCleanup(session_patch.stop)
        self.sessions.return_value.is_running.return_value = False

    def install(self):
        return native_overlay.install(self.service, self.game_id)

    def remove(self):
        return native_overlay.remove(self.service, self.game_id)

    def managed_bytes(self, folder=None):
        folder = folder or self.folder
        return {name: (folder / name).read_bytes() for name in self.managed if (folder / name).is_file()}

    def modify_configs(self):
        changed = {
            "BilingualPanel.ini": (
                "[Panel]\nDatabase=" + str(self.database.resolve()) +
                "\nGameId=" + self.game_id + "\nTitle=用户保留的名称\nOpacity=0.41\n"
                "; user comment must survive\n[User]\nKeep=yes\n"
            ).encode("utf-16"),
            "ReShade.ini": b"[INPUT]\nKeyOverlay=0,0,0,0\n[User]\nKeep=custom-value\n",
        }
        for name, content in changed.items():
            (self.folder / name).write_bytes(content)
        return changed

    def test_install_and_remove_only_owned_files(self):
        self.assertFalse(native_overlay.installed(self.service, self.game_id))
        self.install()
        self.assertTrue(native_overlay.installed(self.service, self.game_id, verify=True))
        self.assertEqual((self.folder / "dxgi.dll").read_bytes(), self.runtime.read_bytes())
        self.assertEqual((self.folder / "BilingualPanel.addon64").read_bytes(), self.addon.read_bytes())
        panel = (self.folder / "BilingualPanel.ini").read_text(encoding="utf-16")
        self.assertIn(self.game_id, panel)
        self.assertIn(str(self.database.resolve()), panel)
        self.assertIn(self.profile["title"], panel)
        self.sessions.assert_called_with(self.executable.resolve())
        self.remove()
        self.assertFalse(native_overlay.installed(self.service, self.game_id))
        self.assertEqual(self.managed_bytes(), {})
        self.assertEqual(self.unrelated.read_bytes(), b"unrelated user content")
        self.assertEqual(self.executable.read_bytes(), game_image())
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["phase"], "disabled")

    def test_godot_dynamically_loaded_d3d12_can_install_in_auto_mode(self):
        image = bytearray(game_image())
        struct.pack_into("<II", image, 152 + 120, 0, 0)
        self.executable.write_bytes(image)
        self.profile["adapter"] = "configured"
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "D3D12", "source": "Godot 游戏配置"}):
            report = native_overlay.inspect(self.service, self.game_id)
            self.assertTrue(report["ready"], report["blockers"])
            self.assertEqual(report["apis"], ["D3D12"])
            self.install()
            self.assertTrue(native_overlay.installed(self.service, self.game_id, verify=True))
            compatibility = self.folder / "override.cfg"
            self.assertIn("agility_sdk_version=999999", compatibility.read_text())
            self.remove()
            self.assertFalse(compatibility.exists())

    def test_existing_godot_override_is_not_overwritten(self):
        self.profile["adapter"] = "configured"
        existing = self.folder / "override.cfg"
        original = b"[rendering]\nrenderer/rendering_method=\"forward_plus\"\n"
        existing.write_bytes(original)
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "D3D12", "source": "Godot 项目配置"}):
            with self.assertRaisesRegex(ValueError, "override.cfg"):
                self.install()
        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(self.managed_bytes(), {})

    def test_old_godot_install_requires_compatibility_reinstall(self):
        self.profile["adapter"] = "configured"
        self.install()  # Simulate the old installer: same binaries, no override.
        before, marker = self.managed_bytes(), self.marker.read_bytes()
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "D3D12", "source": "Godot 项目配置"}):
            report = native_overlay.inspect(self.service, self.game_id)
            self.assertTrue(report["installed"])
            self.assertTrue(report["needs_update"])
            self.assertFalse(report["ready"])
            self.assertIn("兼容配置", "\n".join(report["blockers"]))
            with self.assertRaisesRegex(ValueError, "兼容配置"):
                self.install()
            self.assertEqual(self.managed_bytes(), before)
            self.assertEqual(self.marker.read_bytes(), marker)
            self.remove()
            self.install()
            self.assertFalse(native_overlay.inspect(self.service, self.game_id)["needs_update"])
            self.assertIn("override.cfg", json.loads(self.marker.read_text(encoding="utf-8"))["config_hashes"])

    def test_godot_required_override_is_checked_but_appearance_remains_editable(self):
        self.profile["adapter"] = "configured"
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "D3D12", "source": "Godot 项目配置"}):
            self.install()
            compatibility = self.folder / "override.cfg"
            original = compatibility.read_bytes()
            self.modify_configs()
            self.assertTrue(native_overlay.installed(self.service, self.game_id, verify=True))
            compatibility.unlink()  # Temporary fixture only.
            with self.assertRaisesRegex(ValueError, "override.cfg.*缺失"):
                native_overlay.installed(self.service, self.game_id, verify=True)
            compatibility.write_bytes(original.replace(b"999999", b"614"))
            with self.assertRaisesRegex(ValueError, "override.cfg.*修改"):
                native_overlay.installed(self.service, self.game_id, verify=True)
            compatibility.write_bytes(original)
            self.assertTrue(native_overlay.installed(self.service, self.game_id, verify=True))

    def test_changed_godot_override_is_preserved_but_not_reused_for_install(self):
        self.profile["adapter"] = "configured"
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "D3D12", "source": "Godot 项目配置"}):
            self.install()
            compatibility = self.folder / "override.cfg"
            changed = b"[rendering]\nrendering_device/d3d12/agility_sdk_version=614\n"
            compatibility.write_bytes(changed)
            self.remove()
            self.assertEqual(compatibility.read_bytes(), changed)
            self.assertEqual(self.managed_bytes(), {})
            marker = self.marker.read_bytes()
            report = native_overlay.inspect(self.service, self.game_id)
            self.assertFalse(report["ready"])
            with self.assertRaisesRegex(ValueError, "override.cfg.*未覆盖"):
                self.install()
            self.assertEqual(self.marker.read_bytes(), marker)
            self.assertEqual(compatibility.read_bytes(), changed)
            self.assertEqual(self.managed_bytes(), {})

    def test_godot_vulkan_cannot_be_mislabelled_as_d3d12(self):
        self.profile["adapter"] = "configured"
        with patch("bilingual.native_godot.inspect_renderer", return_value={
                "api": "VULKAN", "source": "启动参数"}):
            for renderer in ("auto", "D3D12"):
                report = native_overlay.inspect(self.service, self.game_id, renderer)
                self.assertFalse(report["ready"])
                self.assertIn("VULKAN", report["blockers"][0])
        self.assertEqual(self.managed_bytes(), {})

    def test_running_game_refuses_install_and_remove_without_changes(self):
        self.sessions.return_value.is_running.return_value = True
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.managed_bytes(), {})
        self.assertFalse(self.marker.exists())
        self.sessions.return_value.is_running.return_value = False
        self.install()
        before, marker = self.managed_bytes(), self.marker.read_bytes()
        self.sessions.return_value.is_running.return_value = True
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual(self.managed_bytes(), before)
        self.assertEqual(self.marker.read_bytes(), marker)

    def test_foreign_proxy_addon_or_configs_are_never_overwritten(self):
        for name in self.managed:
            with self.subTest(existing=name):
                target = self.folder / name
                foreign = ("foreign file: " + name).encode("utf-8")
                target.write_bytes(foreign)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertEqual(self.managed_bytes(), {name: foreign})
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.unrelated.read_bytes(), b"unrelated user content")
                target.unlink()  # Only this test's temporary fixture.

    def test_modified_configs_survive_removal(self):
        self.install()
        changed = self.modify_configs()
        self.remove()
        self.assertEqual(self.managed_bytes(), changed)
        self.assertFalse(native_overlay.installed(self.service, self.game_id))
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["phase"], "disabled")

    def test_preserved_configs_can_be_reinstalled_without_losing_custom_content(self):
        self.install()
        changed = self.modify_configs()
        self.remove()
        self.install()
        self.assertTrue(native_overlay.installed(self.service, self.game_id, verify=True))
        for name, content in changed.items():
            self.assertEqual((self.folder / name).read_bytes(), content)
        # Reusing a retained config does not convert it into a deletable new file.
        self.remove()
        self.assertEqual(self.managed_bytes(), changed)

    def test_changed_preserved_config_is_not_silently_adopted(self):
        self.install()
        self.modify_configs()
        self.remove()
        target = self.folder / "ReShade.ini"
        target.write_bytes(b"[ForeignReplacement]\nDoNotAdopt=yes\n")
        before, marker = self.managed_bytes(), self.marker.read_bytes()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.managed_bytes(), before)
        self.assertEqual(self.marker.read_bytes(), marker)

    def test_preserved_config_ownership_does_not_transfer_to_another_directory(self):
        self.install()
        changed = self.modify_configs()
        self.remove()
        original_folder = self.folder
        other_root = self.root / "another-game-location"
        other_executable = other_root / self.relative_executable
        other_executable.parent.mkdir(parents=True)
        other_executable.write_bytes(b"another-game-fixture")
        for name, content in changed.items():
            (other_executable.parent / name).write_bytes(content)
        self.profile["root"] = str(other_root)
        before_marker = self.marker.read_bytes()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.managed_bytes(other_executable.parent), changed)
        self.assertEqual(self.managed_bytes(original_folder), changed)
        self.assertEqual(self.marker.read_bytes(), before_marker)

    def test_changed_installed_binary_refuses_removal_without_partial_cleanup(self):
        self.install()
        target = self.folder / "dxgi.dll"
        target.write_bytes(b"modified-by-another-program")
        before, marker = self.managed_bytes(), self.marker.read_bytes()
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual(self.managed_bytes(), before)
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(self.unrelated.read_bytes(), b"unrelated user content")

    def test_unsupported_architecture_has_no_side_effects(self):
        self.executable.write_bytes(game_image(0x14c))
        self.assertFalse(native_overlay.inspect(self.service, self.game_id)["ready"])
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.managed_bytes(), {})

    def test_root_level_executable_keeps_language_transaction(self):
        from bilingual.patching import PatchSet
        self.profile["window_executable"] = "NewGame.exe"
        (self.game / "NewGame.exe").write_bytes(game_image())
        source = self.root / "translation.txt"
        source.write_text("bilingual", encoding="utf-8")
        language = PatchSet(self.game, self.game_id)
        language.enable([{"source": str(source), "target": "translation.txt"}])
        before = language.manifest.read_bytes()
        self.install()
        self.assertEqual(language.manifest.read_bytes(), before)
        self.remove()
        self.assertEqual(language.manifest.read_bytes(), before)
        self.assertEqual((self.game / "translation.txt").read_text(encoding="utf-8"), "bilingual")

    def test_saved_native_theme_survives_removal_and_reinstall(self):
        from bilingual.themes import presets
        native_overlay.save_preferences(self.service, self.game_id, presets()["cool"])
        self.install()
        self.assertEqual(native_overlay.theme(self.service, self.game_id)["Source"], "preset:cool")
        native_overlay.save_preferences(self.service, self.game_id, presets()["neutral"])
        self.remove()
        native_overlay.save_preferences(self.service, self.game_id, presets()["warm"])
        self.install()
        self.assertEqual(native_overlay.theme(self.service, self.game_id)["Source"], "preset:warm")

    def test_older_owned_addon_requires_explicit_reinstall(self):
        from bilingual.themes import presets
        self.install()
        before = self.managed_bytes()
        self.addon.write_bytes(b"new-version-of-owned-addon")
        self.assertTrue(native_overlay.inspect(self.service, self.game_id)["needs_update"])
        with self.assertRaises(ValueError):
            native_overlay.save_preferences(self.service, self.game_id, presets()["cool"])
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.managed_bytes(), before)
        self.remove()
        self.install()
        self.assertFalse(native_overlay.inspect(self.service, self.game_id)["needs_update"])


if __name__ == "__main__":
    unittest.main()
