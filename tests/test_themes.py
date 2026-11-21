"""Theme normalization and synthetic local screenshot checks; no game access."""
import colorsys
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bilingual.themes import contrast_ratio, default_theme, normalize_theme, presets, theme_from_image


class ThemeTests(unittest.TestCase):
    def assert_readable(self, theme):
        for foreground in ("Text", "Muted", "Accent"):
            for surface in ("Background", "Panel"):
                self.assertGreaterEqual(contrast_ratio(theme[foreground], theme[surface]), 4.5)

    def test_presets_are_readable_independent_and_match_native_colors(self):
        values = presets()
        self.assertEqual(set(values), {"warm", "cool", "neutral"})
        self.assertEqual(values["warm"]["Accent"], "#D9BD87")
        for name, theme in values.items():
            self.assert_readable(theme)
            self.assertEqual(normalize_theme(theme), theme)
            self.assertEqual(theme["Source"], "preset:" + name)
        values["warm"]["Background"] = "#FFFFFF"
        self.assertEqual(default_theme()["Background"], "#161412")

    def test_hex_opacity_and_unrounded_contrast(self):
        value = normalize_theme({"background": " 123 ", "border": "#aBc", "opacity": "0.10"})
        self.assertEqual(value["Background"], "#112233")
        self.assertEqual(value["Border"], "#AABBCC")
        self.assertEqual(value["Opacity"], 0.35)
        self.assertEqual(normalize_theme({"Opacity": 2})["Opacity"], 0.95)
        self.assertEqual(contrast_ratio("#000", "#FFF"), 21.0)
        self.assertLess(contrast_ratio("#777777", "#FFFFFF"), 4.5)
        self.assertGreater(contrast_ratio("#767676", "#FFFFFF"), 4.5)

    def test_malformed_input_does_not_silently_create_a_theme(self):
        bad_values = [[], {"Text": "red"}, {"Text": "#12345678"}, {"Opacity": True},
                      {"Opacity": float("nan")}, {"Opacity": float("inf")},
                      {"Opacity": "opaque"}, {"Text": "#FFF", "text": "#000"},
                      {"Unknown": "value"}, {"Name": "name\nInjected=1"}]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_theme(value)
        self.assertEqual(normalize_theme(None), default_theme())

    def test_conflicting_surfaces_and_unreadable_text_are_repaired(self):
        value = normalize_theme({"Background": "#333333", "Panel": "#AAAAAA",
                                 "Text": "#333333", "Muted": "#333333", "Accent": "#333333"})
        self.assertEqual(value["Panel"], value["Background"])
        self.assertEqual(value["Text"], "#FFFFFF")
        self.assert_readable(value)
        light = normalize_theme({"Background": "#FFFFFF", "Panel": "#EEEEEE",
                                 "Text": "#FFFFFF", "Muted": "#EEEEEE", "Accent": "#DDDDDD"})
        self.assertEqual(light["Text"], "#000000")
        self.assert_readable(light)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is optional")
    def test_local_screenshot_palette_is_readable_and_source_is_unchanged(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory(prefix="bilingual-theme-") as directory:
            path = Path(directory) / "local-private-screenshot.png"
            image = Image.new("RGB", (100, 100), (15, 50, 35))
            ImageDraw.Draw(image).rectangle((0, 80, 99, 99), fill=(220, 160, 20))
            image.save(path)
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            theme = theme_from_image(path)
            self.assert_readable(theme)
            self.assertEqual(theme["Source"], "local-image")
            self.assertNotIn(str(path), str(theme))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
            accent = tuple(int(theme["Accent"][n:n + 2], 16) / 255 for n in (1, 3, 5))
            self.assertTrue(0.08 < colorsys.rgb_to_hsv(*accent)[0] < 0.17)
            self.assertEqual(theme_from_image(path), theme)

    def test_bad_file_and_missing_optional_dependency_have_clear_errors(self):
        with tempfile.TemporaryDirectory(prefix="bilingual-theme-") as directory:
            path = Path(directory) / "not-image.png"
            with self.assertRaises(ValueError):
                theme_from_image(path)
            path.write_bytes(b"not a screenshot")
            with patch.dict("sys.modules", {"PIL": None}), self.assertRaisesRegex(RuntimeError, "Pillow"):
                theme_from_image(path)
            if importlib.util.find_spec("PIL"):
                with self.assertRaises(ValueError):
                    theme_from_image(path)


if __name__ == "__main__":
    unittest.main()
