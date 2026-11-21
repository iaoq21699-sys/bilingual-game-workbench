"""Read-only renderer detection regression checks, using temporary fake PCKs."""
from pathlib import Path
import struct
import tempfile
import unittest

from bilingual.engines.pck import write
from bilingual.native_godot import inspect_renderer


def project_binary(entries):
    content = bytearray(b"ECFG" + struct.pack("<I", len(entries)))
    for key, value in entries:
        key_bytes = key.encode("utf-8")
        if isinstance(value, str):
            data = value.encode("utf-8")
            payload = struct.pack("<II", 4, len(data)) + data + bytes(-len(data) % 4)
        else:
            payload = value
        content += struct.pack("<I", len(key_bytes)) + key_bytes + struct.pack("<I", len(payload)) + payload
    return bytes(content)


class GodotRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-godot-")
        self.root = Path(self.temp.name)
        self.profile = {"engine": "godot", "root": str(self.root), "executable": "Game.exe", "launch_args": []}
        (self.root / "Game.exe").write_bytes(b"not executed or inspected")

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, entries, name="Game.pck"):
        write(self.root / name, {"project.binary": project_binary(entries)}, engine_version=(4, 5, 1))

    def test_godot_dynamic_directx_import_regression_uses_project_setting(self):
        self.pack([("application/config/name", "Fixture"),
                   ("rendering/rendering_device/driver.windows", "d3d12")])
        result = inspect_renderer(self.profile)
        self.assertEqual(result["api"], "D3D12")
        self.assertIn("project.binary", result["source"])

    def test_explicit_non_directx_driver_wins_and_invalid_does_not_fall_back(self):
        self.pack([("rendering/rendering_device/driver.windows", "d3d12")])
        (self.root / "override.cfg").write_text('[rendering]\nrendering_device/driver.windows="d3d12"\n')
        for args, expected in [(["--rendering-driver", "vulkan"], "VULKAN"),
                               (["--rendering-driver=opengl3"], "OPENGL")]:
            self.profile["launch_args"] = args
            self.assertEqual(inspect_renderer(self.profile)["api"], expected)
        for args in (["--rendering-driver"], ["--rendering-driver=future"],
                     ["--rendering-driver=vulkan", "--rendering-driver=d3d12"]):
            self.profile["launch_args"] = args
            self.assertIsNone(inspect_renderer(self.profile))

    def test_override_priority_unknown_and_unrelated_settings(self):
        self.pack([("rendering/rendering_device/driver.windows", "d3d12")])
        path = self.root / "override.cfg"
        path.write_text('config_version=5\n[rendering]\nrendering_device/driver.windows="vulkan" ; chosen\n')
        self.assertEqual(inspect_renderer(self.profile)["api"], "VULKAN")
        for content in ('[rendering]\nrendering_device/driver.windows="future"\n',
                        '[rendering]\nrendering_device/driver.windows=123\n',
                        '[rendering]\nrendering_device/driver.windows.x86_64="d3d12"\n',
                        '[bad section\n'):
            path.write_text(content)
            self.assertIsNone(inspect_renderer(self.profile))
        path.write_text('[display]\nwindow/size/viewport_width=1280\n')
        self.assertEqual(inspect_renderer(self.profile)["api"], "D3D12")

    def test_unique_language_pack_fallback_and_ambiguous_or_escaping_pack(self):
        self.pack([("rendering/rendering_device/driver.windows", "d3d12")], name="Content.pck")
        self.profile["pairs"] = [{"pack": "Content.pck"}, {"pack": "Content.pck"}]
        self.assertEqual(inspect_renderer(self.profile)["api"], "D3D12")
        for packs in (["Content.pck", "Other.pck"], ["../outside.pck"]):
            self.profile["pairs"] = [{"pack": pack} for pack in packs]
            self.assertIsNone(inspect_renderer(self.profile))

    def test_only_structured_string_property_counts(self):
        fixtures = [
            [("application/config/name", "rendering/rendering_device/driver.windows d3d12")],
            [("rendering/rendering_device/driver.windows", struct.pack("<II", 2, 12))],
            [("rendering/rendering_device/driver.windows", b"\x04\x00\x00\x00\xff\xff\xff\xffd3d12")],
            [("rendering/rendering_device/driver.windows", "d3d12"),
             ("rendering/rendering_device/driver.windows", "vulkan")],
        ]
        for index, entries in enumerate(fixtures):
            name = f"fixture-{index}.pck"
            self.pack(entries, name)
            self.profile["pairs"] = [{"pack": name}]
            self.assertIsNone(inspect_renderer(self.profile))


if __name__ == "__main__":
    unittest.main()
