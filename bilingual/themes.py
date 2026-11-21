"""Local overlay themes; no network, game files, or persistent state are used.

Contrast uses the WCAG sRGB relative-luminance calculation against the two
*opaque* theme surfaces. Transparency can reveal any game frame underneath,
so these checks do not guarantee readability over every scene.
Reference: https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html
Pillow is optional and imported only when extracting a local screenshot.
"""
from __future__ import annotations

import colorsys
import math
from pathlib import Path
import re
import warnings


COLOR_KEYS = ("Background", "Panel", "Text", "Muted", "Accent", "Border")
MIN_OPACITY = 0.35
MAX_OPACITY = 0.95
MIN_TEXT_CONTRAST = 4.5
_KEYS = {key.casefold(): key for key in (*COLOR_KEYS, "Opacity", "Name", "Source")}
_HEX = re.compile(r"#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")
_PRESETS = {
    "warm": {
        "Name": "暖炭金", "Source": "preset:warm", "Opacity": 0.78,
        "Background": "#161412", "Panel": "#211F1B", "Text": "#F2EBDB",
        "Muted": "#A39C8C", "Accent": "#D9BD87", "Border": "#574D3B",
    },
    "cool": {
        "Name": "冷蓝灰", "Source": "preset:cool", "Opacity": 0.78,
        "Background": "#121922", "Panel": "#1C2936", "Text": "#EAF3FA",
        "Muted": "#A1B5C7", "Accent": "#85BFE2", "Border": "#456078",
    },
    "neutral": {
        "Name": "中性灰", "Source": "preset:neutral", "Opacity": 0.78,
        "Background": "#171717", "Panel": "#242424", "Text": "#F1F1F1",
        "Muted": "#ABABAB", "Accent": "#C9C9C9", "Border": "#555555",
    },
}


def _hex(value: object, key: str = "颜色") -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value.strip()):
        raise ValueError(f"{key} 必须是 RGB 十六进制颜色，例如 #D9BD87；不接受颜色透明通道。")
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return "#" + digits.upper()


def _rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _from_rgb(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, channel)) * 255):02X}" for channel in rgb)


def _luminance(color: str) -> float:
    linear = tuple(channel / 12.92 if channel <= 0.04045
                   else ((channel + 0.055) / 1.055) ** 2.4 for channel in _rgb(color))
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the actual, unrounded sRGB contrast ratio, ignoring transparency."""
    first, second = _luminance(_hex(foreground)), _luminance(_hex(background))
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _minimum_contrast(color: str, surfaces: tuple[str, str]) -> float:
    return min(contrast_ratio(color, surface) for surface in surfaces)


def _anchor(surfaces: tuple[str, str]) -> str:
    # Keep this tie rule and protection logic in sync with native/Overlay.cpp.
    return "#FFFFFF" if _minimum_contrast("#FFFFFF", surfaces) >= _minimum_contrast("#000000", surfaces) else "#000000"


def default_theme() -> dict:
    """Return a new, independent copy of the default warm theme."""
    return dict(_PRESETS["warm"])


def presets() -> dict[str, dict]:
    """Return independent warm, cool, and neutral themes, indexed by stable IDs."""
    return {name: dict(value) for name, value in _PRESETS.items()}


def normalize_theme(value: dict | None = None) -> dict:
    """Validate a partial theme and return canonical keys and opaque #RRGGBB.

    Color input accepts RGB or RRGGBB, with an optional leading '#'. Unknown or
    duplicate keys, non-finite opacity, booleans, and malformed colors fail.
    Finite opacity is clamped to 0.35..0.95. Text, Muted, and Accent each need
    4.5:1 against both Background and Panel, otherwise they use readable black
    or white. If those surfaces admit neither anchor, Panel becomes Background.
    Border is decorative and has no enforced contrast threshold.
    """
    if value is None:
        return default_theme()
    if not isinstance(value, dict):
        raise ValueError("主题必须是包含颜色设置的对象。")
    if not value:
        return default_theme()
    supplied = {}
    for raw_key, item in value.items():
        key = _KEYS.get(raw_key.casefold()) if isinstance(raw_key, str) else None
        if key is None:
            raise ValueError(f"不支持的主题设置：{raw_key}")
        if key in supplied:
            raise ValueError(f"主题设置重复：{key}")
        supplied[key] = item
    result = default_theme()
    result.update({"Name": "自定义", "Source": "custom"})
    for key in COLOR_KEYS:
        result[key] = _hex(supplied.get(key, result[key]), key)
    opacity = supplied.get("Opacity", result["Opacity"])
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float, str)):
        raise ValueError("背景不透明度必须是有限数值。")
    try:
        opacity = float(opacity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("背景不透明度必须是有限数值。") from exc
    if not math.isfinite(opacity):
        raise ValueError("背景不透明度必须是有限数值。")
    result["Opacity"] = max(MIN_OPACITY, min(MAX_OPACITY, opacity))
    for key in ("Name", "Source"):
        metadata = supplied.get(key, result[key])
        if not isinstance(metadata, str) or len(metadata) > 240 or any(ord(c) < 32 for c in metadata):
            raise ValueError(f"{key} 必须是不含控制字符的短文本。")
        result[key] = metadata.strip()
    surfaces = (result["Background"], result["Panel"])
    anchor = _anchor(surfaces)
    if _minimum_contrast(anchor, surfaces) < MIN_TEXT_CONTRAST:
        result["Panel"] = result["Background"]
        surfaces = (result["Background"], result["Panel"])
        anchor = _anchor(surfaces)
    for key in ("Text", "Muted", "Accent"):
        if _minimum_contrast(result[key], surfaces) < MIN_TEXT_CONTRAST:
            result[key] = anchor
    return result


def theme_from_image(path: Path) -> dict:
    """Extract a readable dark theme from a local image without modifying it.

    Dominant pixels provide the background hue. A sufficiently represented,
    saturated palette color supplies the accent. Shades are adapted for reading
    and normalized; this is color extraction, not a screenshot readability audit.
    No screenshot bytes or full path are included in the result.
    """
    image_path = Path(path)
    if not image_path.is_file():
        raise ValueError("请选择本机已有的截图文件。")
    if image_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("截图文件过大，请使用不超过 64 MB 的图片。")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("截图取色需要 Pillow；当前环境未安装。仍可使用预设或手动配色。") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_path) as source:
                if source.width * source.height > 50_000_000:
                    raise ValueError("截图尺寸过大，请先缩小至 5000 万像素以内。")
                sample = ImageOps.exif_transpose(source)
                sample.thumbnail((192, 192), Image.Resampling.LANCZOS)
                rgba = sample.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, (22, 20, 18, 255))
                canvas.alpha_composite(rgba)
                quantized = canvas.convert("RGB").quantize(colors=12, method=Image.Quantize.MEDIANCUT)
                palette = quantized.getpalette()
                counts = sorted(quantized.getcolors() or [], key=lambda row: (-row[0], row[1]))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("无法读取这张截图，请选择有效的本地 PNG、JPEG 或其他支持的图片。") from exc
    if not counts or palette is None:
        raise ValueError("截图没有可用于取色的像素。")
    total = sum(count for count, _ in counts)
    colors = [(count / total, tuple(channel / 255 for channel in palette[index * 3:index * 3 + 3]))
              for count, index in counts]
    dominant = colors[0][1]
    hue, saturation, brightness = colorsys.rgb_to_hsv(*dominant)
    background_saturation = min(0.28, saturation * 0.45)
    background_value = 0.08 + brightness * 0.06
    background = _from_rgb(colorsys.hsv_to_rgb(hue, background_saturation, background_value))
    panel = _from_rgb(colorsys.hsv_to_rgb(hue, background_saturation * 0.8, background_value + 0.055))
    candidates = [(fraction, rgb) for fraction, rgb in colors if fraction >= 0.01]
    def accent_score(row):
        fraction, rgb = row
        separation = math.sqrt(sum((channel - base) ** 2 for channel, base in zip(rgb, dominant)))
        return (colorsys.rgb_to_hsv(*rgb)[1] + 0.05) * fraction ** 0.35 * (0.2 + separation)
    _, accent_rgb = max(candidates, key=accent_score)
    accent_hue, accent_saturation, _ = colorsys.rgb_to_hsv(*accent_rgb)
    accent_saturation = 0.0 if accent_saturation < 0.08 else min(0.65, max(0.25, accent_saturation))
    accent_rgb = colorsys.hsv_to_rgb(accent_hue, accent_saturation, 0.88)
    accent = _from_rgb(accent_rgb)
    # Preserve the extracted hue while lightening it enough for small labels.
    for step in range(1, 21):
        if _minimum_contrast(accent, (background, panel)) >= MIN_TEXT_CONTRAST:
            break
        amount = step / 20
        accent = _from_rgb(tuple(channel * (1 - amount) + amount for channel in accent_rgb))
    return normalize_theme({
        "Name": "截图配色", "Source": "local-image", "Opacity": 0.78,
        "Background": background, "Panel": panel,
        "Text": _from_rgb(colorsys.hsv_to_rgb(hue, 0.025, 0.96)),
        "Muted": _from_rgb(colorsys.hsv_to_rgb(hue, 0.10, 0.69)),
        "Accent": accent,
        "Border": _from_rgb(colorsys.hsv_to_rgb(hue, background_saturation, 0.34)),
    })
