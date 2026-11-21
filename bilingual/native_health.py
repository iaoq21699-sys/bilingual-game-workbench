"""Non-blocking, read-only health checks for the current game-process add-on.

Only telemetry bound to the exact image, PID and creation FILETIME can establish
readiness. Log messages, installation records and file presence cannot do so.
terminal describes the current result, not the monitor's lifetime: poll again
after failures, exits or restarts to observe recovery.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from . import native_overlay
from .game_session import GameSession, _canonical


_FILETIME_EPOCH = 116444736000000000
_FLAGS = (("addon_loaded", "插件加载"), ("runtime_ready", "图形挂钩"),
          ("overlay_ready", "绘制回调"), ("input_ready", "键鼠输入"), ("corpus_ready", "语料读取"))
_HEARTBEAT_SECONDS = 15
_MAX_STATUS_BYTES = 64 * 1024
_FINAL_ERRORS = {"config_error", "register_failed", "init_error", "initialization_error", "addon_unloaded"}
_REINSTALL = "退出游戏后，从“游戏 → 移除游戏内检索面板”移除，再进入“游戏内面板与主题”重新安装。"
_DATABASE_ACTION = "在工作台选择这款游戏，使用“资料 → 更新本地文本库”；若项目已移动，再重新安装面板以更新数据库路径。"
_ERRORS = {
    "config_error": ("面板配置读取失败", "插件无法读取本机面板配置或其中的游戏、数据库信息。", [_REINSTALL]),
    "register_failed": ("插件注册失败", "插件未能向 ReShade 完成注册，运行库或插件版本可能不匹配。",
                        ["确认使用 ReShade 6.8.0 full add-on 版本。", _REINSTALL]),
    "db_error": ("本机文本库读取失败", "插件暂时无法打开或读取当前游戏的文本库。", [_DATABASE_ACTION]),
    "query_error": ("游戏文本查询失败", "本次文本或词义查询未能完成。", [_DATABASE_ACTION]),
    "empty_corpus": ("未找到当前游戏的文本", "文本库中尚无当前游戏可用的句子。", [_DATABASE_ACTION]),
    "input_not_ready": ("游戏输入尚未就绪", "面板还未连接到本次游戏窗口的键盘与鼠标输入。",
                        ["进入游戏主菜单后继续等待；若仍未就绪，退出游戏后更新并重新安装面板。"]),
    "render_error": ("面板绘制发生错误", "插件在游戏内绘制面板时遇到问题。",
                     ["退出后检查当前图形接口和面板版本，再重新启动；若重现，保留本次错误详情。"]),
    "init_error": ("面板初始化失败", "插件未能完成本次初始化。", [_REINSTALL]),
    "initialization_error": ("面板初始化失败", "插件未能完成本次初始化。", [_REINSTALL]),
    "addon_unloaded": ("插件已卸载", "本次游戏进程中的插件已停止运行，之前的就绪状态不再有效。", [_REINSTALL]),
    "config_write_error": ("外观设置保存失败", "当前外观修改未能写入本机配置。",
                           ["检查游戏目录是否允许当前用户写入，稍后再次保存外观设置。"]),
}


def _version(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(value))
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _signature(path):
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns, path.is_file()
    except OSError:
        return str(path), None


class NativeHealthMonitor:
    def __init__(self, service, game_id, startup_timeout=90, clock=time.monotonic):
        timeout = float(startup_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("加载检查等待时间必须是正数。")
        self.service, self.game_id = service, str(game_id)
        self.startup_timeout, self.clock = timeout, clock
        self.session = None
        self.executable = self.folder = self.marker_path = None
        self._setup_error = ""
        self._identity = None
        self._observed = self.clock()
        self._initial_age = 0.0
        self._preflight_key = self._preflight_result = None
        self._database_path = None
        self._hashes = {}
        self._last_elapsed = 0.0
        self._configure()

    def _configure(self):
        try:
            _, self.executable, self.folder, self.marker_path = native_overlay._paths(self.service, self.game_id)
            self.session = GameSession(self.executable)
            self._setup_error = ""
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._setup_error = str(exc)

    def _hash(self, path):
        key = _signature(path)
        if key not in self._hashes:
            # Only the small add-on and owned compatibility config are hashed.
            self._hashes = {old: value for old, value in self._hashes.items() if old[0] != key[0]}
            self._hashes[key] = native_overlay.digest(path)
        return self._hashes[key]

    def _preflight(self):
        built = self.service.project / "native/build/bin/Release/BilingualPanel.addon64"
        paths = [self.marker_path, self.executable, built,
                 *(self.folder / name for name in ("dxgi.dll", "BilingualPanel.addon64", "BilingualPanel.ini", "ReShade.ini", "override.cfg")),
                 self.folder / ".bilingual-workbench/state.json",
                 self.folder / ".bilingual-workbench/native-panel/state.json"]
        if self._database_path:
            paths.append(self._database_path)
        key = tuple(_signature(path) for path in paths)
        if key == self._preflight_key:
            return self._preflight_result
        result = None
        try:
            if not self.executable.is_file():
                raise ValueError("实际游戏程序不存在，请检查这款游戏的接入目录。")
            if not native_overlay.installed(self.service, self.game_id, verify=False):
                raise ValueError("尚未安装完整的游戏内面板，请在“游戏内面板与主题”中检查并安装。")
            marker = native_overlay._read_marker(self.marker_path)
            if marker.get("health_protocol") != 1 or _version(marker.get("addon_version")) < (0, 5, 3):
                result = ("needs_update", "内置面板需要更新", "此安装未包含本次启动状态回报功能，不能用旧日志确认加载。",
                          ["退出游戏后移除内置面板，再安装当前版本；双语资源会保留。"])
            addon = self.folder / "BilingualPanel.addon64"
            if result is None and built.is_file() and self._hash(addon) != self._hash(built):
                result = ("needs_update", "内置面板需要更新", "游戏中的插件与工作台当前构建不同。",
                          ["退出游戏后移除并重新安装内置面板。"])
            if result is None:
                for name in ("BilingualPanel.ini", "ReShade.ini"):
                    path = self.folder / name
                    if not path.is_file():
                        raise ValueError(f"缺少面板配置文件：{name}。")
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError(f"面板配置文件异常过大：{name}。")
                expected = marker.get("config_hashes", {}).get("override.cfg")
                if expected and self._hash(self.folder / "override.cfg") != expected:
                    raise ValueError("Godot 兼容配置 override.cfg 已变动，请检查后重新安装面板。")
                config = native_overlay._read_config(self.folder / "BilingualPanel.ini")
                if config.get("Panel", "GameId", fallback="") != self.game_id:
                    raise ValueError("面板配置中的 GameId 与当前游戏不一致。")
                database = config.get("Panel", "Database", fallback="")
                if not database:
                    raise ValueError("面板配置缺少本机数据库路径 Database。")
                path = Path(database)
                self._database_path = (path if path.is_absolute() else self.folder / path).resolve()
                if not self._database_path.is_file():
                    raise ValueError("面板配置中的数据库路径不存在，请重新接入文本库或检查 Database 路径。")
        except Exception as exc:
            detail = str(exc)
            actions = [_DATABASE_ACTION] if "数据库" in detail or "Database" in detail else [_REINSTALL]
            if "GameId" in detail:
                actions = ["确认工作台选中了正确游戏，再重新安装该游戏的面板以重写配置。", _REINSTALL]
            result = ("failed", "面板安装或配置需要修复", detail, actions)
        self._preflight_key, self._preflight_result = key, result
        return result

    def _read_status(self, identity):
        path = self.folder / f"BilingualPanel.status.{identity['pid']}.json"
        try:
            if not path.is_file():
                return None, "尚未收到本次启动的插件状态。"
            if path.stat().st_size > _MAX_STATUS_BYTES:
                return None, "插件状态文件过大，未采信。"
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                return None, "插件状态格式无效，等待下一次回报。"
            if (type(value.get("pid")) is not int or value["pid"] != identity["pid"]
                    or value.get("process_start_filetime") != identity["process_start_filetime"]):
                return None, "发现其他启动留下的状态，已忽略。"
            code = value.get("error_code", "")
            empty_error_game = value.get("game_id") == "" and code in ("config_error", "register_failed")
            if value.get("game_id") != self.game_id and not empty_error_game:
                return None, "状态所属游戏不匹配，未采信。"
            updated = value.get("updated_at_ms")
            started = (int(identity["process_start_filetime"]) - _FILETIME_EPOCH) // 10000
            now = time.time() * 1000
            if type(updated) is not int or updated < started or updated > now + 5000:
                return None, "插件状态时间与本次启动不符，未采信。"
            # Early initialization failures stop the writer. Keep a matching
            # current-process final error as a failure explanation, never ready.
            if now - updated > _HEARTBEAT_SECONDS * 1000 and code not in _FINAL_ERRORS:
                return None, "本次插件状态已超过 15 秒未更新，等待新的回报。"
            if value.get("schema_version") != 1 or _version(value.get("addon_version")) < (0, 5, 3):
                return {"upgrade_required": True}, "插件状态协议或版本不匹配。"
            if any(type(value.get(flag)) is not bool for flag, _ in _FLAGS):
                return None, "插件状态字段不完整，等待下一次回报。"
            if type(value.get("corpus_count")) is not int or type(value.get("panel_drawn")) is not bool:
                return None, "插件状态字段不完整，等待下一次回报。"
            if not isinstance(code, str) or not isinstance(value.get("error_detail", ""), str):
                return None, "插件错误信息格式无效。"
            return value, ""
        except (OSError, ValueError, TypeError):
            return None, "暂时无法读取插件状态，等待下一次回报。"

    def _report(self, state, title, detail, actions=(), identity=None, telemetry=None, elapsed=0.0):
        steps = [{"label": "游戏进程", "state": "ready" if identity else "waiting",
                  "detail": f"已核对程序路径、PID {identity['pid']} 和启动时间" if identity else "尚未发现正在运行的对应游戏"}]
        config_error = self._preflight_result[2] if self._preflight_result else self._setup_error
        steps.append({"label": "安装与配置", "state": "failed" if config_error else "ready",
                      "detail": config_error or "已检查本机面板文件与数据库路径"})
        for flag, label in _FLAGS:
            ready = bool(telemetry and telemetry.get(flag) is True)
            step_detail = "已就绪" if ready else "等待本次启动回报"
            if flag == "corpus_ready" and ready:
                step_detail = f"已读取 {telemetry['corpus_count']:,} 条游戏文本"
            steps.append({"label": label, "state": "ready" if ready else "waiting", "detail": step_detail})
        drawn = bool(telemetry and telemetry.get("panel_drawn") is True)
        steps.append({"label": "面板显示", "state": "ready" if drawn else "waiting",
                      "detail": "已检测到面板显示" if drawn else "尚未检测到实际显示；就绪后按 F8"})
        return {"state": state, "title": title, "detail": detail, "steps": steps, "actions": list(actions),
                "pid": identity["pid"] if identity else None, "elapsed": round(max(0.0, elapsed), 1),
                "terminal": state in {"ready", "failed", "exited", "needs_update"}, "panel_drawn": drawn}

    def poll(self) -> dict:
        """Take one bounded snapshot; safe to call again after any result."""
        if self.session is None:
            self._configure()  # A corrected profile can recover without a new monitor.
        if self._setup_error or self.session is None:
            return self._report("failed", "游戏接入配置需要修复", self._setup_error,
                                ["检查实际游戏程序路径，再重新打开加载检查。"])
        try:
            identity = self.session.process_identity()
        except OSError as exc:
            return self._report("failed", "无法确认游戏进程", str(exc), ["确认游戏与工作台在同一 Windows 用户下运行。"])
        if identity and (_canonical(identity.get("exe_path", "")) != _canonical(self.executable)
                         or type(identity.get("pid")) is not int
                         or not re.fullmatch(r"[0-9]+", str(identity.get("process_start_filetime", "")))):
            identity = None
        preflight = self._preflight()
        if identity:
            identity = dict(identity, process_start_filetime=str(identity["process_start_filetime"]))
            token = (identity["pid"], identity["process_start_filetime"])
            if self._identity != token:
                self._identity = token
                self._observed = self.clock()
                started = (int(identity["process_start_filetime"]) - _FILETIME_EPOCH) / 10_000_000
                self._initial_age = max(0.0, time.time() - started)
            self._last_elapsed = max(0.0, self.clock() - self._observed + self._initial_age)
        if preflight:
            return self._report(*preflight, identity=identity, elapsed=self._last_elapsed if identity else 0)
        if not identity:
            if self._identity is not None:
                return self._report("exited", "游戏已退出", "已停止等待上一次启动；再次启动游戏后会重新检查。", elapsed=self._last_elapsed)
            return self._report("waiting", "等待游戏启动", "尚未发现正在运行的对应游戏；启动后会自动检查加载状态。")
        elapsed = self._last_elapsed
        value, note = self._read_status(identity)
        if value and value.get("upgrade_required"):
            return self._report("needs_update", "内置面板需要更新", note,
                                ["退出游戏后安装与工作台匹配的当前插件。"], identity, elapsed=elapsed)
        if value and value.get("error_code"):
            code = value["error_code"][:80]
            title, explanation, actions = _ERRORS.get(code, ("插件报告问题", "插件报告了本次运行问题，具体原因请结合错误详情检查。", [_REINSTALL]))
            state = "starting" if code == "input_not_ready" and elapsed < self.startup_timeout else "failed"
            if code == "config_write_error" and all(value[flag] for flag, _ in _FLAGS) and value["corpus_count"] > 0:
                title = "已加载，外观保存失败"
            raw = value.get("error_detail", "")[:600]
            detail = explanation + (f" 技术详情：{raw}（{code}）" if raw else f"（{code}）")
            return self._report(state, title, detail, actions, identity, value, elapsed)
        if value and all(value[flag] for flag, _ in _FLAGS) and value["corpus_count"] > 0:
            detail = ("已检测到面板显示。" if value["panel_drawn"] else "插件、语料、绘制回调与输入均已就绪，尚未检测到面板实际显示。")
            return self._report("ready", "面板已加载，按 F8 打开", detail, identity=identity, telemetry=value, elapsed=elapsed)
        missing = next(((flag, label) for flag, label in _FLAGS if not value or not value.get(flag)), ("corpus_ready", "语料读取"))
        if elapsed < self.startup_timeout:
            detail = note or f"正在等待{missing[1]}完成。"
            return self._report("starting", "正在检查面板加载", detail, identity=identity, telemetry=value, elapsed=elapsed)
        if not value:
            detail = note + " 当前版本插件尚未报告本次加载结果，不能据此确定失败根因。"
            actions = ["确认从已配置的实际游戏程序启动；退出游戏后检查或重新安装内置面板。",
                       "若仍无状态，请查看本次启动的 ReShade.log；旧日志不能证明本次已加载。"]
        else:
            detail = f"游戏已运行 {elapsed:.0f} 秒，{missing[1]}仍未就绪。"
            actions = {
                "addon_loaded": ["退出游戏后检查 ReShade full add-on 运行库与插件版本。"],
                "runtime_ready": ["检查游戏当前图形接口及所需兼容配置；尚未建立图形挂钩不等于已确定具体原因。"],
                "overlay_ready": ["进入游戏主菜单后继续检查；若仍无绘制回调，检查本次启动的图形运行日志。"],
                "input_ready": ["确认已进入游戏窗口；若输入仍未就绪，退出后检查插件版本。"],
                "corpus_ready": ["检查本机文本库是否存在且包含当前游戏，再查看插件报告的数据库错误。"],
            }[missing[0]]
        return self._report("failed", f"{missing[1]}尚未完成" if value else "未收到本次插件加载状态",
                            detail, actions, identity, value, elapsed)
