"""Read-only Electron graphics preflight; loaded modules are not proof of drawing.

Only standard process/window metadata and named graphics-library imports are
observed. This does not change renderer selection or install/load an add-on.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .game_session import GameSession
from .native_target import inspect_executable
from .patching import inside

_LIBRARIES = ("libGLESv2.dll", "libEGL.dll", "vk_swiftshader.dll", "vulkan-1.dll")
_GRAPHICS = ("d3d10.dll", "d3d11.dll", "d3d12.dll", "dxgi.dll", "opengl32.dll",
             "vulkan-1.dll", "vk_swiftshader.dll", "libglesv2.dll", "libegl.dll")


def _loaded_modules(executable):
    if os.name != "nt":
        return {"state": "unsupported", "processes": []}
    session = GameSession(executable)
    identities = {pid: session.process_identity(pid) for pid in session.process_ids()}
    identities = {pid: identity for pid, identity in identities.items() if identity}
    if not identities:
        return {"state": "not_running", "processes": []}
    numbers = ",".join(str(pid) for pid in sorted(identities))
    allowed = ",".join("'" + name + "'" for name in _GRAPHICS)
    command = (
        "$ErrorActionPreference='Stop';$allowed=@(" + allowed + ");"
        "$rows=@(foreach($n in @(" + numbers + ")){try{"
        "$p=Get-Process -Id $n -ErrorAction Stop;"
        "$m=@($p.Modules|ForEach-Object {$_.ModuleName.ToLowerInvariant()}|"
        "Where-Object {$allowed -contains $_}|Sort-Object -Unique);"
        "[pscustomobject]@{pid=$n;ok=$true;modules=$m}"
        "}catch{[pscustomobject]@{pid=$n;ok=$false;modules=@()}}});"
        "ConvertTo-Json -InputObject $rows -Compress -Depth 4"
    )
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, timeout=8,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            return {"state": "unavailable", "processes": []}
        rows = json.loads(completed.stdout.decode("utf-8-sig"))
        if not isinstance(rows, list):
            return {"state": "unavailable", "processes": []}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unavailable", "processes": []}
    observed = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int:
            continue
        pid = row["pid"]
        if pid not in identities or session.process_identity(pid) != identities[pid]:
            continue
        modules = row.get("modules", [])
        if not isinstance(modules, list):
            modules = []
        observed.append(dict(pid=pid, ok=bool(row.get("ok")),
                             modules=sorted({name for name in modules if name in _GRAPHICS})))
    return {"state": "observed" if any(row["ok"] for row in observed) else "unavailable",
            "processes": observed}


def inspect(profile, observe_runtime=False):
    if not isinstance(profile, dict) or profile.get("engine") != "electron":
        return None
    root = Path(profile["root"]).resolve()
    executable = inside(root, profile["window_executable"])
    static = {}
    for name in _LIBRARIES:
        path = inside(root, name)
        if path.is_file():
            try:
                static[name] = inspect_executable(path)["apis"]
            except (OSError, ValueError):
                continue
    evidence = {"libraries": static, "runtime": _loaded_modules(executable) if observe_runtime else
                {"state": "not_checked", "processes": []}, "active_api": None,
                "panel_verified": False}
    description = ["Electron/Chromium 渲染架构"]
    hints = sorted({api for apis in static.values() for api in apis})
    if hints:
        description.append("图形库静态依赖 " + ", ".join(hints))
    runtime = evidence["runtime"]
    if runtime["state"] == "observed":
        modules = sorted({m for row in runtime["processes"] for m in row["modules"]})
        if modules:
            description.append("运行时加载 " + ", ".join(modules))
        else:
            description.append("未观察到可识别的图形模块")
    elif runtime["state"] == "not_running":
        description.append("未检测到可验证的游戏进程")
    elif runtime["state"] == "unavailable":
        description.append("运行时模块信息不可读取")
    evidence["source"] = "；".join(description) + "（不是实际绘制接口的证明）"
    return evidence
