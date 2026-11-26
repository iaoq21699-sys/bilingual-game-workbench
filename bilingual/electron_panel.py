"""Configure and observe the workbench's Electron in-game panel."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from .engines import electron, electron_red_pearls
from .engines.asar import Archive
from .game_session import GameSession
from .patching import inside, save_json

ASSETS = Path(__file__).with_name("electron_web")


def _folder(service, game_id):
    if not re.fullmatch(r"game-[a-f0-9]{16}", game_id):
        raise ValueError("无效的 Electron 游戏标识。")
    return service.data_dir / "electron_panels" / game_id


def enabled(service, game_id):
    settings = _folder(service, game_id) / "settings.json"
    return settings.is_file() and json.loads(settings.read_text(encoding="utf-8")).get("enabled") is True


def save_appearance(service, game_id, theme, card_side):
    """The running web panel stores its appearance in workbench data."""
    from . import native_overlay
    from .themes import normalize_theme

    if not isinstance(theme, dict) or "Opacity" not in theme or card_side not in ("left", "right"):
        raise ValueError("游戏内面板的外观设置不完整。")
    theme = normalize_theme(theme)
    native_overlay._save_local_preferences(service, game_id, theme, "auto", card_side)
    return {"theme": theme, "cardSide": card_side}


@contextmanager
def launch_lock(folder):
    if os.name != "nt":
        raise ValueError("Electron 游戏内面板当前支持 Windows。")
    import msvcrt

    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "session.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("这款游戏的双语会话正在启动或运行。") from exc
        yield


def installation(profile):
    if os.name != "nt":
        raise ValueError("Electron 游戏内面板当前支持 Windows。")
    root = Path(profile["root"])
    executable = inside(root, profile["executable"])
    if executable != inside(root, profile["window_executable"]) or not executable.is_file():
        raise ValueError("Electron 面板需要直接启动实际游戏程序，请重新检查游戏 EXE。")
    path = electron._path(root)
    if not path or profile.get("archive") != path.relative_to(root).as_posix():
        raise ValueError("游戏的 Electron ASAR 位置有变化，请重新接入游戏目录。")
    archive = Archive(path)
    metadata = electron._metadata(archive)
    if not metadata.get("name") or metadata != profile.get("package"):
        raise ValueError("Electron 游戏包的名称或版本有变化，请重新接入游戏目录。")
    schema = electron_red_pearls if metadata["name"] == electron_red_pearls.PACKAGE else None
    candidates = {name for name, entry in archive.entries.items() if entry.offset is not None
                  and PurePosixPath(name).suffix.lower() in {".html", ".htm"}
                  and not set(PurePosixPath(name).parts).intersection({"node_modules", "vendor"})}
    pages = candidates & {schema.PAGE} if schema else candidates
    if not pages:
        raise ValueError("未找到已支持的 Electron 本地 HTML 游戏页面；仍可使用桌面检索。")
    signature = profile.get("report", {}).get("archive_signature")
    if signature is None:
        # Profiles written before the renderer backend have no archive stamp.
        current = electron.build(root, profile, None)["entries"]
        if current != json.loads(Path(profile["entries_file"]).read_text(encoding="utf-8")):
            raise ValueError("Electron 语言数据有变化，请重新接入游戏目录。")
    elif signature != list(archive.signature):
        raise ValueError("Electron ASAR 已更新，请重新接入游戏目录以刷新语料。")
    archive.check_unchanged()
    return executable, archive, pages, schema


def page_name(url, archive_path, pages):
    try:
        target = urlsplit(url)
        archive = urlsplit(archive_path.as_uri())
    except ValueError:
        return None
    if target.scheme != "file" or target.netloc.casefold() != archive.netloc.casefold():
        return None
    path = unquote(target.path, errors="replace")
    prefix = unquote(archive.path).rstrip("/") + "/"
    name = path[len(prefix):]
    if path[:len(prefix)].casefold() == prefix.casefold() and name in pages:
        return name
    return None


def inspect(service, profile, renderer=None):
    game_id = profile["id"]
    active = enabled(service, game_id)
    root = Path(profile["root"])
    executable = inside(root, profile["executable"])
    running = GameSession(executable).is_running()
    with service._db() as db:
        count = db.execute("SELECT count(*) FROM entries WHERE game_id=?", (game_id,)).fetchone()[0]
    blockers = []
    try:
        installation(profile)
        if not all((ASSETS / name).is_file() for name in ("panel.js", "panel.css", "inline.js", "inline.css")):
            raise ValueError("工作台缺少 Electron 面板文件，请检查程序目录。")
    except (OSError, ValueError) as exc:
        blockers.append(str(exc))
    if renderer not in (None, "auto"):
        blockers.append("Electron 面板使用网页渲染，请使用自动识别。")
    if not count:
        blockers.append("请先接入这款游戏的双语语料。")
    if running:
        blockers.append("请先退出游戏，再保存设置。")
    return dict(backend="electron", game_id=game_id, title=profile["title"], executable=str(executable),
                directory=str(root), renderer="auto", count=count, installed=active, running=running,
                needs_update=False, blockers=blockers, ready=not blockers,
                compatibility="由本次游戏启动后的加载检查确认")


def install(service, profile):
    with launch_lock(_folder(service, profile["id"])):
        report = inspect(service, profile)
        if not report["ready"]:
            raise ValueError("\n".join(report["blockers"]))
        save_json(_folder(service, profile["id"]) / "settings.json", {"enabled": True})
    return "已启用游戏内双语和检索面板。请从工作台启动游戏，按 F8 打开面板。"


def remove(service, profile):
    folder = _folder(service, profile["id"])
    with launch_lock(folder):
        if GameSession(inside(profile["root"], profile["executable"])).is_running():
            raise ValueError("请先退出游戏，再关闭这款游戏的内置面板。")
        save_json(folder / "settings.json", {"enabled": False})
    return "已关闭这款游戏的内置面板。双语语料仍可在工作台检索。"


def read_status(folder):
    path = folder / "runtime.json"
    try:
        if path.stat().st_size <= 8192:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return {}


def launch(service, profile):
    folder = _folder(service, profile["id"])
    request = uuid4().hex
    helper = subprocess.Popen(
        [sys.executable, "-m", "bilingual.electron_runtime", str(service.data_dir.resolve()), profile["id"], request],
        cwd=service.project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = read_status(folder)
        if state.get("request") == request:
            if state.get("state") == "failed" and not state.get("pid"):
                raise ValueError(state["detail"])
            if state.get("pid") or state.get("state") == "exited":
                return "已启动游戏并检查内置面板。面板就绪后按 F8。"
        if helper.poll() is not None:
            raise ValueError("启动 Electron 双语会话失败，请查看工作台的面板运行检查。")
        time.sleep(0.08)
    return "已请求启动 Electron 游戏，请查看面板运行检查。"


class HealthMonitor:
    def __init__(self, service, game_id):
        self.service = service
        self.game_id = game_id
        self.folder = _folder(service, game_id)
        self.session = GameSession(service.session_info(game_id)["window_executable"])

    def poll(self):
        state, title, detail = "waiting", "等待从工作台启动游戏", "从工作台启动后会自动检查游戏内面板。"
        steps, actions, pid, drawn = [], [], None, False
        if not enabled(self.service, self.game_id):
            state, title, detail = "not_installed", "尚未启用游戏内面板", "可在面板设置中启用。"
            actions = ["打开「游戏内面板与主题」，检查并启用。"]
        else:
            value = read_status(self.folder)
            saved_pid = value.get("pid")
            identity = self.session.process_identity(saved_pid) if type(saved_pid) is int else None
            matched = bool(identity and identity["process_start_filetime"] == value.get("process_start_filetime"))
            if matched:
                pid, state, detail = saved_pid, value["state"], value.get("detail", "")
                if state == "ready" and time.time() - value.get("updated", 0) > 15:
                    state, detail = "failed", "游戏仍在运行，已超过 15 秒没有收到面板状态。"
                title = {"ready": "游戏内面板已就绪", "starting": "正在连接 Electron 游戏页面",
                         "failed": "Electron 面板加载失败"}.get(state, title)
                ready = state == "ready"
                drawn = ready and value.get("panel_drawn") is True
                steps = [dict(label="游戏进程", state="ready", detail=f"已核对程序路径、PID {pid} 和启动时间"),
                         dict(label="游戏页面", state="ready" if ready else state, detail=detail),
                         dict(label="按键与语料", state="ready" if ready else "waiting",
                              detail="F8 可用，可检索本机双语语料。" if ready else "等待游戏页面就绪。"),
                         dict(label="面板显示", state="ready" if drawn else "waiting",
                              detail="已收到页面的实际显示回报。" if drawn else "就绪后按 F8 打开。")]
            elif self.session.is_running():
                detail = "检测到普通游戏会话。退出后从工作台启动即可加载内置面板。"
                title = "游戏已通过普通入口启动"
                actions = ["退出该游戏，再用工作台的「启动游戏」。"]
            elif value.get("state") == "failed":
                state, title, detail = "failed", "上次 Electron 面板未能加载", value.get("detail", "")
            elif value.get("state") == "starting" and time.time() - value.get("updated", 0) < 20:
                state, title, detail = "starting", "正在启动游戏", value.get("detail", "")
            elif value.get("state") in ("ready", "exited"):
                state, title = "exited", "游戏已退出"
            if state == "failed":
                actions = ["退出游戏后从工作台重试。Steam 更新游戏后可重新接入游戏目录。",
                           "游玩时也可按 Ctrl+Shift+B 使用桌面检索。"]
        return dict(backend="electron", state=state, title=title, detail=detail, steps=steps, actions=actions,
                    pid=pid, elapsed=0, terminal=state in {"ready", "failed", "exited"}, panel_drawn=drawn)
