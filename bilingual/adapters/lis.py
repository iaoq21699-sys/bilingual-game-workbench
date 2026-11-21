"""Local adapter for the existing LiS Remastered Build 11330282 project.

Reading never installs a patch or changes the source game. The optional lifecycle
commands deliberately delegate to the existing, version-checked patch manager.
"""

from __future__ import annotations

from collections import defaultdict
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys


GAME_DIRECTORY = "Life.is.Strange.Remastered.Build.11330282"
LOCALIZATION = Path("LIS/Content/Packages/Localization")
# Same record grammar as the existing project's bilingual_ui.records parser.
_RECORD = re.compile(
    r'^\[(?P<section>[^\r\n\]]+)\][ \t]*(?=\r?$)|'
    r'^(?P<key>[^\s;=\[\]][^\r\n=]*?)[ \t]*=[ \t]*"(?P<value>.*?)"[ \t]*(?=\r?$)',
    re.MULTILINE | re.DOTALL,
)
_UNTERMINATED = {"HUD.ini": "TutoHIM4", "Menus.ini": "ExitConfirm"}


def _project(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    if (root / "scripts/reference_data.json").is_file():
        return root
    if root.name == GAME_DIRECTORY and (root.parent / "scripts/reference_data.json").is_file():
        return root.parent
    raise ValueError("未找到奇异人生复刻版的现有双语项目及 reference_data.json。")


def detect(root: Path) -> bool:
    try:
        project = _project(root)
        return (project / GAME_DIRECTORY / "LiS.exe").is_file()
    except (OSError, ValueError):
        return False


def _clean(value: str) -> str:
    if not isinstance(value, str):
        return ""
    if "Text not localized" in value:
        return ""
    return html.unescape(
        value.replace("[action_new_phrase]", "\n")
        .replace(r"\r\n", "\n").replace(r"\n", "\n")
        .replace(r'\"', '"').replace("\r\n", "\n")
    ).strip()


def _records(path: Path) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """Keep repeated keys and original identities; never pair by line position."""
    result = defaultdict(list)
    if not path.is_file():
        return result
    source = path.read_text(encoding="utf-8-sig")
    missing_quote = _UNTERMINATED.get(path.name) if path.parent.name == "zh-Hans" else None
    if missing_quote:
        source = re.sub(rf'(?m)^({missing_quote}="[^"\r\n]*)(?=\r?$)', r'\1"', source, count=1)
    section = ""
    for match in _RECORD.finditer(source):
        if match["section"] is not None:
            section = match["section"]
        else:
            key = match["key"]
            result[(section.casefold(), key.casefold())].append((section, key, _clean(match["value"])))
    return result


def _category(filename: str, section: str, key: str) -> str:
    filename, section, key = filename.casefold(), section.casefold(), key.casefold()
    if section == "diary":
        return "人物档案" if key.startswith("chara_") else "日记"
    if section == "sms":
        return "短信"
    if section == "sp":
        return "阅读文本"
    if section == "tuto":
        return "教程"
    if "objective" in filename:
        return "任务"
    if "endcredits" in key:
        return "演职员表"
    if "dircomsubs" in key:
        return "导演评论"
    return "界面与互动"


def load(root: Path) -> dict:
    project = _project(root)
    reference = project / "scripts/reference_data.json"
    data = json.loads(reference.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("不支持的奇异人生词句索引格式。")
    entries = []
    keys = set()
    for original in data["entries"]:
        key = "dialogue:" + str(original["id"])
        if key in keys:
            raise ValueError(f"奇异人生对白索引含重复 ID：{original['id']}")
        keys.add(key)
        entries.append({
            "key": key,
            "namespace": f"第 {original.get('episode', 0)} 章 / {original.get('scene', '')}",
            "category": "过场对白" if str(original["id"]).startswith("VTT:") else "对白",
            "en": _clean(original.get("en", "")),
            "zh": _clean(original.get("zh", "")),
            "speaker": original.get("speaker", ""),
            "source": f"{reference}#{original['id']}",
            "note": original.get("note", ""),
            "phrases": [
                {"term": gloss["headword"], "meaning": gloss["definition"], "source": "原双语项目 · 上下文释义"}
                for gloss in original.get("glosses", [])
                if gloss.get("headword") and gloss.get("definition")
            ],
        })

    dialogue_count = len(entries)
    localization = project / "scripts/.work/original" / LOCALIZATION
    files = sorted({p.name for culture in ("en", "zh-Hans") for p in (localization / culture).glob("*.ini")})
    duplicate_count = 0
    for filename in files:
        # The curated index already contains the CU_ dialogue, with better notes
        # and corrected translations. Do not add duplicate subtitle records.
        if filename.split("__")[-1].startswith("CU_"):
            continue
        english = _records(localization / "en" / filename)
        chinese = _records(localization / "zh-Hans" / filename)
        for identity in sorted(english.keys() | chinese.keys()):
            en_values, zh_values = english.get(identity, []), chinese.get(identity, [])
            # Repeated same-key resources are ambiguous. Retain every variant as
            # one-language records instead of guessing the translation pairing.
            ambiguous = len(en_values) > 1 or len(zh_values) > 1
            if ambiguous:
                variants = [(row, None, f"en-{i}") for i, row in enumerate(en_values)]
                variants += [(None, row, f"zh-{i}") for i, row in enumerate(zh_values)]
                duplicate_count += len(variants)
            else:
                variants = [(en_values[0] if en_values else None, zh_values[0] if zh_values else None, "")]
            for en_row, zh_row, suffix in variants:
                section, original_key, _ = en_row or zh_row
                en, zh = en_row[2] if en_row else "", zh_row[2] if zh_row else ""
                if not en and not zh:
                    continue
                key = f"ini:{filename}/{identity[0]}/{identity[1]}" + (f"/{suffix}" if suffix else "")
                if key in keys:
                    raise ValueError(f"本地化文本 ID 冲突：{key}")
                keys.add(key)
                note = "同一文本键有多个版本，保留单语言记录，未猜测配对。" if ambiguous else ""
                if not en or not zh:
                    note += "此原始资源缺少" + ("英文" if not en else "中文") + "，保留可用原文。"
                entries.append({
                    "key": key,
                    "namespace": f"{filename} / {section}",
                    "category": _category(filename, section, original_key),
                    "en": en, "zh": zh, "speaker": "",
                    "source": f"{localization}/{filename}#[{section}]{original_key}（en / zh-Hans）",
                    "note": note, "phrases": [],
                })

    extra_count = len(entries) - dialogue_count
    missing_count = sum(not row["en"] or not row["zh"] for row in entries)
    notes_count = sum(bool(row["phrases"]) for row in entries)
    coverage = (
        f"已导入 {dialogue_count:,} 条对白与过场，以及 {extra_count:,} 条原始本地化文本"
        f"（菜单、日记、阅读文本等）；{notes_count:,} 句带原项目上下文释义。"
        f"保留 {missing_count:,} 条缺语言记录。未覆盖贴图文字、未收录的运行时生成文本；"
        "不代表游戏所有可见文本已完整收录。"
    )
    if not files:
        coverage += "未找到已有原始本地化提取目录，本次只导入现有对白索引。"
    if duplicate_count:
        coverage += f"另有 {duplicate_count} 条重复键变体未强行配对。"
    return {
        "id": "lis-remastered", "title": "奇异人生：复刻版",
        "root": str(project), "adapter": "lis", "coverage": coverage, "entries": entries,
    }


def action(root: Path, action: str) -> str:
    """Explicit UI actions only. Loading/detecting never calls this function."""
    if action not in {"status", "enable", "disable", "launch"}:
        raise ValueError(f"不支持的操作：{action}")
    project = _project(root)
    game = project / GAME_DIRECTORY
    executable = game / "LiS.exe"
    manager = project / "scripts/manage_bilingual.py"
    if not executable.is_file() or not manager.is_file():
        raise ValueError("找不到已适配的 Build 11330282 游戏或原补丁管理器；未执行操作。")
    if action == "launch":
        # Launch without the legacy companion so it cannot compete for F8.
        subprocess.Popen([str(executable)], cwd=str(game))
        return "已启动奇异人生；本工具的检索面板独立运行。"
    mode = {"status": "status", "enable": "notes", "disable": "restore"}[action]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")
    try:
        completed = subprocess.run(
            [str(interpreter), "-B", str(manager), mode], cwd=str(manager.parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=environment, timeout=30 if action == "status" else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("读取补丁状态超过 30 秒，未确认状态。") from exc
    if completed.returncode:
        message = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(message or f"原补丁管理器失败，退出码 {completed.returncode}。")
    return completed.stdout.strip() or "操作完成。"
