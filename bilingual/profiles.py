"""Data-configured game onboarding, reversible installation, and launch entries."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .game_session import GameSession
from .patching import PatchSet, inside, save_json

ENGINES = ("electron", "unreal", "godot", "loose_json")


class Profiles:
    def __init__(self, service):
        self.service = service
        self.home = service.data_dir / "profiles"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tools_dir = service.project / "tools"

    def _path(self, game_id):
        if not re.fullmatch(r"game-[0-9a-f]{16}", game_id):
            raise ValueError("无效的游戏配置标识。")
        return self.home / (game_id + ".json")

    def load(self, game_id):
        return json.loads(self._path(game_id).read_text(encoding="utf-8"))

    @staticmethod
    def engine(name):
        if name not in ENGINES:
            raise ValueError("此配置的资源格式尚不支持。")
        return importlib.import_module("bilingual.engines." + name)

    @classmethod
    def corpus_only(cls, profile):
        # This is a code-owned adapter capability, not an imported-config flag.
        return bool(getattr(cls.engine(profile["engine"]), "CORPUS_ONLY", False))

    def inspect(self, root):
        root = Path(root).resolve()
        if not root.is_dir():
            raise ValueError("请选择已经解压或安装的游戏目录。")
        configuration = root / "bilingual-game.json"
        if configuration.is_file():
            return self.read(configuration, root)
        errors = []
        for name in ENGINES:
            try:
                result = self.engine(name).probe(root, self.tools_dir)
                if result and result.get("pairs"):
                    return self.validate(result)
                if result and result.get("recognized") and self.corpus_only(result):
                    # A recognized container without safe language pairs is a
                    # useful diagnostic, not an unknown game or an installable profile.
                    return self.validate(result, allow_unpaired=True)
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                errors.append(str(exc))
        raise ValueError("没有识别到可自动接入的中英文资源。\n可导入已有接入配置，或选择松散 JSON 的两种语言文件。\n"
                         + "\n".join(dict.fromkeys(errors))[:1600])

    def validate(self, candidate, allow_unpaired=False):
        profile = copy.deepcopy(candidate)
        root = Path(profile["root"]).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("配置中的游戏目录不存在，请重新选择目录。")
        engine = self.engine(profile.get("engine"))
        corpus_only = bool(getattr(engine, "CORPUS_ONLY", False))
        profile["corpus_only"] = corpus_only
        profile["root"] = str(root)
        profile["title"] = str(profile.get("title") or root.name).strip()
        choices = profile.get("executables") or [p.name for p in root.glob("*.exe")]
        if not choices:
            raise ValueError("没有找到游戏 EXE，请选择直接包含游戏程序的目录。")
        for choice in choices:
            inside(root, choice)
        profile["executables"] = choices
        profile.setdefault("executable", choices[0])
        # Prefer a rendering process over Unreal's root bootstrap executable.
        profile.setdefault("window_executable", next((p for p in choices if "-Shipping.exe" in p), profile["executable"]))
        for key in ("executable", "window_executable"):
            path = inside(root, profile[key])
            if not path.is_file() or path.suffix.lower() != ".exe":
                raise ValueError(f"配置的游戏程序不存在：{profile[key]}")
        args = profile.get("launch_args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise ValueError("启动参数必须是文本数组。")
        if corpus_only and args:
            raise ValueError("Electron 接入由工作台管理运行参数，接入配置中的 launch_args 必须为空。")
        profile["launch_args"] = args
        if not isinstance(profile.get("pairs"), list) or (not profile["pairs"] and not (corpus_only and allow_unpaired)):
            raise ValueError("未配置中英文语言资源。")
        profile["connectable"] = bool(profile["pairs"])
        profile.setdefault("warnings", [])
        profile.setdefault("options", {})
        profile.setdefault("source_locale", "en")
        profile.setdefault("target_locale", "zh")
        profile["id"] = "game-" + hashlib.sha256((str(root).casefold() + "|" + profile["engine"]).encode()).hexdigest()[:16]
        profile["schema_version"] = 1
        return profile

    def read(self, path, root=None):
        path = Path(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("接入配置超过 4 MB。")
        from .importers import load_json
        profile = load_json(path)
        if not isinstance(profile, dict) or profile.get("schema_version", 1) != 1:
            raise ValueError("接入配置格式不受支持。")
        if root is not None:
            profile["root"] = str(Path(root).resolve())
        elif not profile.get("root") or profile["root"] == ".":
            profile["root"] = ""
            return profile
        return self.validate(profile)

    def export(self, game_id, path):
        profile = self.load(game_id)
        result = {key: value for key, value in profile.items() if key not in (
            "id", "root", "build", "files", "fingerprints", "entries_file", "report", "launcher")}
        result["root"] = "."
        destination = Path(path).resolve()
        if destination.suffix.lower() != ".json":
            raise ValueError("接入配置请保存为 JSON 文件。")
        if destination.is_file():
            raise ValueError("此文件已存在；请选择新文件名，避免覆盖已有配置。")
        save_json(destination, result)
        return str(destination)

    def _running(self, profile):
        # Launchers and rendering processes are checked separately.
        for executable in {profile["executable"], profile["window_executable"]}:
            if GameSession(inside(profile["root"], executable)).is_running():
                return True
        return False

    def connect(self, candidate):
        profile = self.validate(candidate)
        root = Path(profile["root"])
        corpus_only = self.corpus_only(profile)
        patch = None
        if not corpus_only:
            if self._running(profile):
                raise ValueError("请先退出这款游戏，再生成或切换双语资源。")
            patch = PatchSet(root, profile["id"])
            if patch.state()["phase"] != "disabled":
                raise ValueError("此游戏已有安装记录，请先用游戏管理菜单恢复原版，再重新接入。")
        workdir = self.home / profile["id"] / ("build-" + uuid.uuid4().hex[:12])
        workdir.mkdir(parents=True)
        result = self.engine(profile["engine"]).build(root, profile, workdir, self.tools_dir)
        files = result.get("files", [])
        if corpus_only and files:
            raise ValueError("只读语料适配器不能生成游戏安装文件。")
        if not result.get("entries") or (not corpus_only and not files):
            raise ValueError("没有生成可靠的双语文本，游戏未修改。")
        for file in files:
            source = Path(file["source"]).resolve()
            if workdir.resolve() not in source.parents:
                raise ValueError("资源生成器返回了生成目录以外的文件。")
            inside(root, file["target"])
        profile.update(build=str(workdir), files=files,
                       fingerprints=result.get("fingerprints", []), report=result.get("report", {}))
        profile["entries_file"] = str(workdir / "entries.json")
        save_json(profile["entries_file"], result["entries"])
        save_json(self._path(profile["id"]), profile)
        # Register before installing, so a failed install remains visible and recoverable.
        self.service.config.setdefault("profiles", [])
        if profile["id"] not in self.service.config["profiles"]:
            self.service.config["profiles"].append(profile["id"])
        self.service._save_config()
        summary = self.service._store(self.payload(profile, result["entries"]))
        if corpus_only:
            self.service._extend_dictionary()
            summary.update(path=str(root), report=profile["report"], corpus_only=True,
                           message="双语语料已接入。可在面板设置中启用原位双语和 F8 内置检索。\n"
                                   "Ctrl+Shift+B 可呼出桌面检索。")
            return summary
        if self._running(profile):
            raise ValueError("双语文件已生成，但游戏在生成期间启动了。请退出游戏，再在管理菜单中启用双语。")
        status = patch.enable(profile["files"], profile["fingerprints"])
        launcher = self.create_launcher(profile["id"])
        self.service._extend_dictionary()
        summary.update(launcher=launcher, path=str(root), report=profile["report"],
                       message=f"{status}\n已生成双语启动入口：{launcher}\n游玩中按 Ctrl+Shift+B 呼出检索，Esc 返回游戏。")
        return summary

    def payload(self, profile, entries=None):
        if entries is None:
            entries = json.loads(Path(profile["entries_file"]).read_text(encoding="utf-8"))
        report = profile.get("report", {})
        if self.corpus_only(profile):
            coverage = str(report.get("coverage") or f"本机 Electron 只读语料：{len(entries):,} 条文本。")
            coverage = coverage.removesuffix("游戏仍为原版显示。")  # v0.9.0 profiles already on disk
        else:
            coverage = (f"从本机 {profile['engine']} 资源生成；收录 {len(entries):,} 条文本。"
                        f"游戏语言应选择 {profile['target_locale']}。" + str(report.get("coverage", "")))
        return dict(id=profile["id"], title=profile["title"], root=profile["root"],
                    adapter="configured", entries=entries, coverage=coverage)

    def session_info(self, game_id):
        profile = self.load(game_id)
        return dict(executable=str(inside(profile["root"], profile["executable"])),
                    window_executable=str(inside(profile["root"], profile["window_executable"])),
                    launch_args=profile["launch_args"], root=profile["root"])

    def action(self, game_id, action):
        profile = self.load(game_id)
        if self.corpus_only(profile):
            from . import electron_panel
            active = electron_panel.enabled(self.service, game_id)
            if action == "status":
                return "已启用游戏内双语 · 请从工作台启动" if active else "已接入双语语料 · 游戏保持原版显示"
            if action != "launch":
                raise ValueError("请在「面板设置」中管理 Electron 游戏内双语。")
            info = self.session_info(game_id)
            if self._running(profile):
                GameSession(info["window_executable"]).bring_to_front()
                return ("游戏已在运行。请查看面板运行检查，普通启动的会话可用 Ctrl+Shift+B。"
                        if active else "游戏已经在运行，桌面双语检索已连接。")
            if active:
                return electron_panel.launch(self.service, profile)
            normal_store = getattr(self.engine(profile["engine"]), "steam_appid", None)
            appid = normal_store(profile["root"]) if normal_store else None
            if appid and os.name == "nt":
                os.startfile("steam://rungameid/" + appid)
                return "已请求 Steam 正常启动游戏；游玩时 Ctrl+Shift+B 呼出桌面双语检索。"
            subprocess.Popen([info["executable"]], cwd=info["root"])
            return "已发出正常启动请求，Ctrl+Shift+B 呼出桌面双语检索；Esc 返回游戏。"
        patch = PatchSet(profile["root"], game_id)
        if action == "status":
            return patch.status()
        if self._running(profile):
            if action == "launch":
                GameSession(inside(profile["root"], profile["window_executable"])).bring_to_front()
                return "游戏已经在运行，检索面板已连接。"
            raise ValueError("请先退出游戏，再切换双语模式。")
        if action == "disable":
            return patch.disable()
        if action not in ("enable", "launch"):
            raise ValueError("未知游戏操作。")
        status = patch.enable(profile["files"], profile["fingerprints"])
        if action == "launch":
            info = self.session_info(game_id)
            # Arguments are passed directly to the game, never to a shell.
            subprocess.Popen([info["executable"], *info["launch_args"]], cwd=info["root"])
            from .native_overlay import installed
            if installed(self.service, game_id):
                return "已启动双语游戏，F8 打开游戏内半透明检索；Esc 收起。"
            return "已启动双语游戏，Ctrl+Shift+B 呼出检索；Esc 返回游戏。"
        return status

    def create_launcher(self, game_id):
        profile = self.load(game_id)
        if self.corpus_only(profile):
            raise ValueError("只读语料接入不在游戏目录创建双语快捷方式；请使用工作台启动或正常打开游戏。")
        if os.name != "nt":
            raise ValueError("当前启动入口仅支持 Windows。")
        executable = Path(sys.executable)
        if executable.name.lower() == "python.exe" and executable.with_name("pythonw.exe").is_file():
            executable = executable.with_name("pythonw.exe")
        script = self.service.project / "main.py"
        destination = inside(profile["root"], "双语启动-" + game_id + ".lnk")
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        args = subprocess.list2cmdline([str(script), "--launch", game_id])
        command = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quote(destination) + ");"
                   "if((Test-Path -LiteralPath " + quote(destination) + ") -and "
                   "(($s.TargetPath -ne " + quote(executable) + ") -or ($s.Arguments -ne " + quote(args) + "))){"
                   "throw 'Shortcut belongs to another target'};"
                   "$s.TargetPath=" + quote(executable) + ";$s.Arguments=" + quote(args) + ";"
                   "$s.WorkingDirectory=" + quote(self.service.project) + ";"
                   "$s.Description='双语游戏与随游戏检索面板';$s.Save()")
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not destination.is_file():
            raise RuntimeError("双语资源已接入，但启动快捷方式创建失败；可从工作台启动游戏。")
        return str(destination)
