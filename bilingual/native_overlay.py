"""Install the process-local ReShade panel without replacing unrelated files."""
from __future__ import annotations

import configparser
import io
import json
import os
import re
import uuid
from pathlib import Path

from .game_session import GameSession
from .patching import PatchSet, digest, inside, save_json
from .native_target import inspect_executable

# The desktop can ship visual updates without rebuilding the game plug-in.
NATIVE_ADDON_VERSION = "0.7.0"

_GODOT_D3D12_CONFIG = (
    "; Bilingual Workbench: use Godot's system D3D12 loader fallback.\n"
    "; Managed by the native panel installer; remove the panel to restore.\n"
    "[rendering]\nrendering_device/d3d12/agility_sdk_version=999999\n"
).encode("utf-8")


def _matches_godot_compatibility(path):
    return (path.is_file() and path.stat().st_size == len(_GODOT_D3D12_CONFIG)
            and path.read_bytes() == _GODOT_D3D12_CONFIG)


def _game(service, game_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", game_id):
        raise ValueError("游戏标识无效。")
    with service._db() as db:
        row = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if row is None:
        raise ValueError("请先把这款游戏的双语文本加入工作台。")
    return dict(row)


def _preference_path(service, game_id):
    _game(service, game_id)
    return service.data_dir / "native_preferences" / (game_id + ".json")


def preferences(service, game_id):
    path = _preference_path(service, game_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _save_local_preferences(service, game_id, appearance, renderer, card_side=None):
    saved = preferences(service, game_id)
    saved.update(version=1, renderer=renderer, theme=appearance)
    if card_side is not None:
        saved["card_side"] = card_side
    save_json(_preference_path(service, game_id), saved)


def _paths(service, game_id):
    profile = _game(service, game_id)
    info = service.session_info(game_id)
    executable = Path(info["window_executable"]).resolve()
    root = Path(profile["root"]).resolve()
    if root not in executable.parents or executable.suffix.lower() != ".exe":
        raise ValueError("实际游戏程序必须位于已接入游戏的目录中。")
    folder = executable.parent
    marker = service.data_dir / "native_profiles" / (game_id + ".json")
    return profile, executable, folder, marker


def _read_marker(marker):
    if not marker.is_file():
        return None
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("version") not in (1, 2) or value.get("backend") != "reshade":
        raise ValueError("内置面板安装记录无法识别。")
    return value


def _patch(folder, game_id, marker):
    # Version 1 used the folder root. New installs have their own transaction
    # directory so a root-level game EXE cannot collide with language patches.
    state_dir = ".bilingual-workbench" if marker and marker.get("version") == 1 else ".bilingual-workbench/native-panel"
    return PatchSet(folder, game_id + ":native-overlay", state_dir=state_dir)


def _read_config(path):
    data = Path(path).read_bytes()
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(data.decode(encoding))
    return config


def theme(service, game_id):
    from .themes import normalize_theme
    saved = preferences(service, game_id).get("theme", {})
    _, _, folder, marker_path = _paths(service, game_id)
    marker = _read_marker(marker_path)
    path = folder / "BilingualPanel.ini"
    if marker and os.path.normcase(marker["directory"]) == os.path.normcase(str(folder)) and path.is_file():
        # The game can save appearance changes itself. Prefer its owned config.
        owned = marker.get("phase") == "enabled" or digest(path) == marker.get("preserved_config_hashes", {}).get(path.name)
        if owned:
            config = _read_config(path)
            saved = dict(config["Theme"]) if config.has_section("Theme") else dict(saved)
            saved["Opacity"] = config.get("Panel", "Opacity", fallback="0.78")
    return normalize_theme(saved)


def save_preferences(service, game_id, appearance, renderer="auto"):
    from .themes import normalize_theme
    if renderer not in ("auto", "D3D10", "D3D11", "D3D12"):
        raise ValueError("请选择自动检测或 DirectX 10 / 11 / 12。")
    profile, executable, folder, marker_path = _paths(service, game_id)
    if GameSession(executable).is_running():
        raise ValueError("请在游戏内调整并保存外观，或退出游戏后从工作台设置。")
    appearance = normalize_theme(appearance)
    marker = _read_marker(marker_path)
    path = folder / "BilingualPanel.ini"
    retained = bool(marker and marker.get("phase") == "disabled" and path.is_file() and digest(path) == marker.get("preserved_config_hashes", {}).get(path.name))
    if marker and (marker.get("phase") == "enabled" or retained):
        if marker.get("phase") == "enabled":
            installed(service, game_id, verify=True)
            built = service.project / "native/build/bin/Release/BilingualPanel.addon64"
            if built.is_file() and digest(folder / "BilingualPanel.addon64") != digest(built):
                raise ValueError("这款游戏仍使用旧版面板，请先移除内置面板，再重新安装新版；双语资源会保留。")
        elif os.path.normcase(marker["directory"]) != os.path.normcase(str(folder)):
            raise ValueError("游戏目录已变化，未修改配置。")
        config = _read_config(path)
        if config.get("Panel", "GameId", fallback="") != game_id:
            raise ValueError("面板配置所属游戏发生变化，未覆盖。")
        config["Theme"] = {key: str(value) for key, value in appearance.items() if key != "Opacity"}
        config["Panel"]["Opacity"] = str(appearance["Opacity"])
        output = io.StringIO()
        config.write(output)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(output.getvalue().encode("utf-16"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if retained:
            marker["preserved_config_hashes"][path.name] = digest(path)
            save_json(marker_path, marker)
    _save_local_preferences(service, game_id, appearance, renderer)
    return "已保存这款游戏的面板外观；下次启动生效。"


def inspect(service, game_id, renderer=None):
    profile, executable, folder, marker_path = _paths(service, game_id)
    configured = service._profiles().load(game_id) if profile.get("adapter") == "configured" else None
    if configured and configured.get("engine") == "electron":
        from . import electron_panel
        return electron_panel.inspect(service, configured, renderer)
    blockers = []
    details = inspect_executable(executable)
    renderer_source = "游戏程序导入表" if details["apis"] else ""
    detected_renderer = None
    if configured:
        from .native_godot import inspect_renderer
        evidence = inspect_renderer(configured)
        if evidence:
            detected_renderer = evidence["api"]
            renderer_source = evidence["source"]
            # Godot loads its graphics libraries dynamically. Its selected
            # driver is stronger evidence than an empty/static PE import list.
            details["apis"] = [detected_renderer]
    renderer = renderer or preferences(service, game_id).get("renderer", "auto")
    if renderer not in ("auto", "D3D10", "D3D11", "D3D12"):
        raise ValueError("不支持这个图形接口。")
    if details["architecture"] != "x64":
        blockers.append("当前面板只支持 64 位 Windows 游戏。")
    if detected_renderer and detected_renderer not in ("D3D10", "D3D11", "D3D12"):
        blockers.append(f"游戏当前使用 {detected_renderer}，当前内置面板安装方式不支持此接口；选择下拉项不会切换游戏接口。")
    elif detected_renderer and renderer not in ("auto", detected_renderer):
        blockers.append(f"游戏配置使用 {detected_renderer}，请将图形接口选为 auto 或 {detected_renderer}。")
    elif not details["apis"] and renderer == "auto":
        blockers.append("尚未识别图形接口，暂不能安装；若已确认游戏使用 DirectX 10/11/12，请在上方选择对应接口。")
    marker = _read_marker(marker_path)
    active = installed(service, game_id, verify=True) if marker and marker.get("phase") == "enabled" else False
    built = service.project / "native/build/bin/Release/BilingualPanel.addon64"
    addon_update = bool(active and built.is_file() and digest(folder / "BilingualPanel.addon64") != digest(built))
    compatibility_update = bool(active and detected_renderer == "D3D12"
                                and "override.cfg" not in (marker or {}).get("config_hashes", {}))
    needs_update = addon_update or compatibility_update
    if addon_update:
        blockers.append("这款游戏的面板有新版：请先移除内置面板，再重新安装，之后可使用启动后的自动加载检查。")
    if compatibility_update:
        blockers.append("这款游戏的旧安装缺少 Godot D3D12 兼容配置：请先移除内置面板，再重新安装；双语资源会保留。")
    if marker and os.path.normcase(marker["directory"]) != os.path.normcase(str(folder)):
        blockers.append("游戏目录已变化，需要先处理原目录安装记录。")
    if not active:
        preserved = (marker or {}).get("preserved_config_hashes", {})
        names = ["dxgi.dll", "BilingualPanel.addon64", "BilingualPanel.ini", "ReShade.ini"]
        if detected_renderer == "D3D12":
            names.append("override.cfg")
        for name in names:
            path = folder / name
            if path.exists() and not (path.is_file() and preserved.get(name) == digest(path)):
                blockers.append("已有其他文件，未覆盖：" + name)
            elif path.exists() and name == "override.cfg" and not _matches_godot_compatibility(path):
                blockers.append("已保留的 override.cfg 与所需兼容配置不同，未覆盖；请先保留并移走该文件，再重新安装。")
    for path, label in ((service.project / "tools/reshade-runtime/ReShade64.dll", "本机 ReShade 运行库"),
                        (service.project / "native/build/bin/Release/BilingualPanel.addon64", "内置面板")):
        if not path.is_file():
            blockers.append("缺少" + label)
    with service._db() as db:
        count = db.execute("SELECT count(*) FROM entries WHERE game_id=?", (game_id,)).fetchone()[0]
    if not count:
        blockers.append("请先导入这款游戏的文本。")
    running = GameSession(executable).is_running()
    if running:
        blockers.append("请先退出游戏，再安装或保存工作台设置。")
    verified = bool(active and not needs_update and (marker or {}).get("verified")
                    and marker.get("verified_executable_sha256") == digest(executable)
                    and marker.get("verified_addon_sha256") == digest(folder / "BilingualPanel.addon64"))
    return {"game_id": game_id, "title": profile["title"], "executable": str(executable), "directory": str(folder),
            **details, "renderer": renderer, "renderer_source": renderer_source, "detected_renderer": detected_renderer,
            "count": count, "installed": active, "running": running, "needs_update": needs_update,
            "blockers": blockers, "ready": not blockers,
            "compatibility": "已实机验证" if verified else "待首次游戏内验证"}


def installed(service, game_id, verify=False):
    marker = service.data_dir / "native_profiles" / (game_id + ".json")
    if not marker.is_file():
        return False
    value = _read_marker(marker)
    if value.get("phase") != "enabled":
        return False
    _, _, folder, _ = _paths(service, game_id)
    if os.path.normcase(str(folder)) != os.path.normcase(value["directory"]):
        raise ValueError("游戏目录已变化，请重新配置内置检索面板。")
    patch = _patch(folder, game_id, value)
    state = patch.state()
    if state.get("phase") != "enabled":
        return False
    for row in state["files"]:
        target = inside(folder, row["target"])
        if not target.is_file():
            raise ValueError("内置面板文件缺失，请重新安装。")
        if verify and digest(target) != row["after"]:
            raise ValueError("内置面板文件被修改，请检查后重新安装。")
    if not (folder / "BilingualPanel.ini").is_file():
        raise ValueError("内置面板配置缺失，请重新安装。")
    # Appearance and ReShade settings are editable. This loader compatibility
    # setting is required to make an installed Godot D3D12 panel render at all.
    config_hashes = value.get("config_hashes", {})
    if "override.cfg" in config_hashes:
        compatibility = inside(folder, "override.cfg")
        if not compatibility.is_file():
            raise ValueError("Godot 兼容配置 override.cfg 缺失，请先移除内置面板，再重新安装。")
        if verify and digest(compatibility) != config_hashes["override.cfg"]:
            raise ValueError("Godot 兼容配置 override.cfg 已被修改，未覆盖；请检查后移除并重新安装面板。")
    return True


def status(service, game_id):
    return "游戏内半透明检索已启用 · F8 打开/收起" if installed(service, game_id) else "尚未安装游戏内检索"


def _panel_config(service, game_id, title, previous=None):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    if previous:
        config.read_string(previous)
    if not config.has_section("Panel"):
        config.add_section("Panel")
    config["Panel"]["Database"] = str(service.database.resolve())
    config["Panel"]["GameId"] = game_id
    config["Panel"]["Title"] = title.replace("\n", " ").replace("\r", " ")
    config["Panel"].setdefault("Opacity", "0.78")
    from .themes import normalize_theme
    appearance = normalize_theme(preferences(service, game_id).get("theme"))
    config["Panel"]["Opacity"] = str(appearance["Opacity"])
    config["Theme"] = {key: str(value) for key, value in appearance.items() if key != "Opacity"}
    output = io.StringIO()
    config.write(output)
    # GetPrivateProfileStringW uses the UTF-16 BOM for paths and Chinese titles.
    return output.getvalue().encode("utf-16")


def _reshade_config():
    font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    return ("[INPUT]\nKeyOverlay=0,0,0,0\nKeyEffects=0,0,0,0\n"
            "KeyScreenshot=0,0,0,0\nInputProcessing=2\n\n"
            "[OVERLAY]\nTutorialProgress=4\nShowClock=0\nShowFPS=0\n"
            "ShowFrameTime=0\nShowPresetName=0\nShowScreenshotMessage=0\n"
            "ShowPresetTransitionMessage=0\nLanguage=zh-CN\n\n"
            f"[STYLE]\nFont={font}\nLatinFont={font}\nFontSize=20\nFontScale=1.2\n\n"
            "[GENERAL]\nPerformanceMode=1\nSkipLoadingDisabledEffects=1\n").encode("utf-8")


def install(service, game_id):
    profile, executable, folder, marker = _paths(service, game_id)
    if profile.get("adapter") == "configured":
        configured = service._profiles().load(game_id)
        if configured.get("engine") == "electron":
            from . import electron_panel
            return electron_panel.install(service, configured)
    if GameSession(executable).is_running():
        raise ValueError("请先退出游戏，再安装游戏内检索面板。")
    previous = _read_marker(marker)
    if previous and os.path.normcase(previous["directory"]) != os.path.normcase(str(folder)):
        raise ValueError("游戏目录已变化，请先处理原目录的内置面板安装记录。")
    report = inspect(service, game_id)
    if previous and previous.get("phase") == "enabled":
        if not report["installed"]:
            raise ValueError("内置面板安装记录不完整，请先移除后重新安装。")
        if report["blockers"]:
            raise ValueError("\n".join(report["blockers"]))
        return "游戏内检索已经安装。启动游戏后按 F8。"
    if report["blockers"]:
        raise ValueError("\n".join(report["blockers"]))
    runtime = service.project / "tools" / "reshade-runtime" / "ReShade64.dll"
    addon = service.project / "native" / "build" / "bin" / "Release" / "BilingualPanel.addon64"
    if not runtime.is_file() or not addon.is_file():
        raise ValueError("缺少原生面板构建或本机 ReShade 运行文件，请先完成 native/build.ps1。")
    if not service.database.is_file():
        raise ValueError("请先建立本机游戏文本库。")
    # Never replace a user's existing ReShade or another proxy/mod loader.
    configs = {"BilingualPanel.ini": _panel_config(service, game_id, profile["title"]),
               "ReShade.ini": _reshade_config()}
    if report["detected_renderer"] == "D3D12":
        # Godot's device factory bypasses ReShade 6.8's D3D12CreateDevice hook.
        # Requesting an unavailable optional Agility SDK uses Godot's existing
        # system-loader fallback, keeping D3D12 and the game's graphics settings.
        # This local override is owned/restored just like our other config files.
        configs["override.cfg"] = _GODOT_D3D12_CONFIG
    reused = {}
    for name in configs:
        path = folder / name
        if path.exists():
            owned_hash = (previous or {}).get("preserved_config_hashes", {}).get(name)
            if path.is_file() and owned_hash and digest(path) == owned_hash:
                if name == "override.cfg" and not _matches_godot_compatibility(path):
                    raise ValueError("已保留的 override.cfg 与所需兼容配置不同，未覆盖；请先保留并移走该文件，再重新安装。")
                reused[name] = owned_hash
                continue
            raise ValueError(f"游戏目录已有 {name}，未覆盖。请先保留已有配置，再选择兼容的接入方式。")
    patch = _patch(folder, game_id, previous)
    if patch.state()["phase"] not in ("disabled",):
        raise ValueError("已有未结束的内置面板安装，请先移除并检查。")
    created = []
    hashes = dict(reused)
    try:
        for name, data in configs.items():
            if name in reused:
                continue
            path = folder / name
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            created.append(path)
            hashes[name] = digest(path)
        patch.enable([{"source": str(runtime), "target": "dxgi.dll"},
                      {"source": str(addon), "target": "BilingualPanel.addon64"}])
        value = {"version": (previous or {}).get("version", 2), "backend": "reshade", "phase": "enabled", "game_id": game_id,
                 "directory": str(folder), "runtime_version": "6.8.0", "addon_version": NATIVE_ADDON_VERSION,
                 "health_protocol": 1, "config_hashes": hashes,
                 "preserved_config_hashes": reused, "hotkey": "F8", "renderer": report["renderer"], "verified": False}
        save_json(marker, value)
    except BaseException:
        if patch.state().get("phase") == "enabled":
            patch.disable()
        for path in reversed(created):
            if path.is_file() and digest(path) == hashes.get(path.name):
                path.unlink()
        raise
    return "已安装游戏内半透明检索。启动游戏后按 F8；无需运行桌面检索窗口。"


def remove(service, game_id):
    profile, executable, folder, marker = _paths(service, game_id)
    if profile.get("adapter") == "configured":
        configured = service._profiles().load(game_id)
        if configured.get("engine") == "electron":
            from . import electron_panel
            return electron_panel.remove(service, configured)
    if GameSession(executable).is_running():
        raise ValueError("请先退出游戏，再移除游戏内检索面板。")
    state = _read_marker(marker)
    if not state or state.get("phase") == "disabled":
        return "当前未安装游戏内检索面板。"
    if os.path.normcase(state["directory"]) != os.path.normcase(str(folder)):
        raise ValueError("游戏目录已变化，未移除任何文件。")
    patch = _patch(folder, game_id, state)
    patch.disable()
    preserved = {}
    for name, original_hash in state.get("config_hashes", {}).items():
        path = inside(folder, name)
        if not path.exists():
            continue
        if path.is_file() and digest(path) == original_hash and name not in state.get("preserved_config_hashes", {}):
            path.unlink()
        else:
            preserved[name] = digest(path) if path.is_file() else ""
    save_json(marker, dict(state, phase="disabled", preserved_config_hashes=preserved))
    note = "；已保留运行中更新的配置：" + "、".join(preserved) if preserved else ""
    return "已移除游戏内检索，原有双语资源仍保留" + note

