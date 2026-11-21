"""Bounded, read-only Godot renderer evidence for native-panel onboarding.

No executable is run or scanned. An absent, ambiguous, or unsupported setting
returns None; a recognized renderer is evidence, not a compatibility guarantee.
"""
from __future__ import annotations

import configparser
import json
from pathlib import Path
import struct

from .engines.pck import Archive
from .patching import inside


_APIS = {"d3d12": "D3D12", "vulkan": "VULKAN", "opengl3": "OPENGL", "opengl3_angle": "OPENGL"}
_ABSENT = object()
_PREFIX = "rendering/rendering_device/driver"
_WINDOWS_DRIVER = _PREFIX + ".windows"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


def _result(driver, source):
    api = _APIS.get(driver) if isinstance(driver, str) else None
    return {"api": api, "source": source} if api else None


def _launch_driver(arguments):
    if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
        return None
    found = []
    for index, argument in enumerate(arguments):
        if argument == "--rendering-driver":
            if index + 1 >= len(arguments):
                return None
            found.append(arguments[index + 1])
        elif argument.startswith("--rendering-driver="):
            found.append(argument.partition("=")[2])
    if not found:
        return _ABSENT
    return found[0] if len(set(found)) == 1 else None


def _override_driver(path):
    if not path.exists():
        return _ABSENT
    if not path.is_file() or path.stat().st_size > _MAX_CONFIG_BYTES:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=True,
                                       delimiters=("=",), inline_comment_prefixes=(";",))
    parser.optionxform = str
    # A Godot config may contain config_version before its first named section.
    parser.read_string("[__global__]\n" + path.read_text(encoding="utf-8-sig"))
    if not parser.has_section("rendering"):
        return _ABSENT
    values = dict(parser["rendering"])
    keys = [key for key in values if key.startswith("rendering_device/driver")]
    if not keys:
        return _ABSENT
    allowed = ("rendering_device/driver", "rendering_device/driver.windows")
    if any(key not in allowed for key in keys):
        return None  # Unhandled feature-qualified override: do not guess.
    key = allowed[1] if allowed[1] in values else allowed[0]
    value = json.loads(values[key])
    return value if isinstance(value, str) else None


def _binary_driver(raw):
    """Read ECFG entries and decode only an exact Godot Variant::STRING (4)."""
    if len(raw) < 8 or len(raw) > _MAX_CONFIG_BYTES or raw[:4] != b"ECFG":
        return None
    count = struct.unpack_from("<I", raw, 4)[0]
    if count > 16384:
        return None
    position = 8
    seen = set()
    drivers = {}
    for _ in range(count):
        if position + 4 > len(raw):
            return None
        key_size = struct.unpack_from("<I", raw, position)[0]
        position += 4
        if not 0 < key_size <= 65536 or position + key_size + 4 > len(raw):
            return None
        key = raw[position:position + key_size].decode("utf-8")
        position += key_size
        value_size = struct.unpack_from("<I", raw, position)[0]
        position += 4
        if position + value_size > len(raw) or key in seen:
            return None
        seen.add(key)
        payload = raw[position:position + value_size]
        position += value_size
        if key.startswith(_PREFIX):
            if key not in (_PREFIX, _WINDOWS_DRIVER):
                return None
            if len(payload) < 8 or struct.unpack_from("<I", payload)[0] != 4:
                return None
            size = struct.unpack_from("<I", payload, 4)[0]
            padded = (size + 3) & ~3
            if size > 128 or len(payload) != 8 + padded or any(payload[8 + size:]):
                return None
            drivers[key] = payload[8:8 + size].decode("utf-8")
    if position != len(raw):
        return None
    # Windows-specific settings override the generic property in Godot.
    return drivers.get(_WINDOWS_DRIVER, drivers.get(_PREFIX))


def _pack(profile, root):
    executable = profile.get("window_executable") or profile.get("executable")
    if not isinstance(executable, str):
        return None
    candidate = inside(root, executable).with_suffix(".pck")
    if candidate.is_file():
        # Resolve again after changing suffix, including a possible PCK symlink.
        return inside(root, candidate.relative_to(root).as_posix())
    pairs = profile.get("pairs", [])
    if not isinstance(pairs, list):
        return None
    packs = {row.get("pack") for row in pairs if isinstance(row, dict) and isinstance(row.get("pack"), str)}
    if len(packs) != 1:
        return None
    candidate = inside(root, next(iter(packs)))
    return candidate if candidate.suffix.lower() == ".pck" and candidate.is_file() else None


def inspect_renderer(profile: dict) -> dict | None:
    """Return {'api': D3D12|VULKAN|OPENGL, 'source': Chinese text}, or None.

    Precedence: explicit launch_args --rendering-driver, root/override.cfg,
    then project.binary in the actual EXE's same-name PCK (or the unique pack
    declared by language pairs). Unknown explicit values and unreadable local
    overrides prevent falling back to an earlier project default.
    """
    if not isinstance(profile, dict) or profile.get("engine") != "godot":
        return None
    try:
        driver = _launch_driver(profile.get("launch_args", []))
        if driver is not _ABSENT:
            return _result(driver, "启动参数 --rendering-driver")
        root = Path(profile["root"]).resolve()
        if not root.is_dir():
            return None
        driver = _override_driver(inside(root, "override.cfg"))
        if driver is not _ABSENT:
            return _result(driver, "本地 override.cfg 图形接口设置")
        pack = _pack(profile, root)
        if pack is None:
            return None
        archive = Archive(pack)
        entry = archive.entries.get("project.binary")
        if entry is None or entry.size > _MAX_CONFIG_BYTES:
            return None
        return _result(_binary_driver(archive.read("project.binary")), "Godot 项目配置 project.binary")
    except (OSError, ValueError, KeyError, TypeError, struct.error, configparser.Error):
        return None
