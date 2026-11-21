"""Import the existing local Subnautica 2 bilingual resources and manage its patch.

No game archive is unpacked here. All writes from actions are limited to the three
owned localization patch files; importing this module or calling load is read-only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import uuid


GAME_ID = "subnautica-2"
BUILD = 138154
PACKAGE_NAMES = tuple("Subnautica2-Bilingual_100_P" + ext for ext in (".pak", ".utoc", ".ucas"))
DOMAIN_TERMS = {"fabricator", "tadpole", "biomod", "biolock", "pda", "noa", "leviathan", "powercell", "bioprint"}
CATEGORIES = {"dialogue": "对话与字幕", "databank": "资料与记录", "items": "物品与生态", "ui": "界面与提示", "other": "其他文本"}
_ACTION_LOCK = threading.RLock()


def _root(root: Path) -> Path:
    path = Path(root).expanduser().resolve()
    return path.parent if path.name.lower() == "bilingualmod" else path


def detect(root: Path) -> bool:
    base = _root(root)
    return (base / "BilingualMod/panel/data.js").is_file() and (base / "version.json").is_file()


def _json(path: Path):
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    return json.loads(raw.decode(encoding))


def _js_data(path: Path, variable: str):
    """Read a known JSON assignment without executing JavaScript."""
    text = path.read_text(encoding="utf-8-sig").strip()
    prefix = "window." + variable + "="
    if not text.startswith(prefix):
        raise ValueError(f"数据格式不匹配：{path.name}")
    return json.loads(text[len(prefix):].removesuffix(";").strip())


def _locres(path: Path) -> list[dict]:
    """Read extracted locres v1-v3, preserving namespace/key/source identity."""
    raw = path.read_bytes()
    stream = io.BytesIO(raw)

    def read_exact(n):
        if n < 0 or n > len(raw):
            raise ValueError(f"语言表长度异常：{path.name}")
        value = stream.read(n)
        if len(value) != n:
            raise ValueError(f"语言表不完整：{path.name}")
        return value

    def number(fmt):
        return struct.unpack("<" + fmt, read_exact(struct.calcsize("<" + fmt)))[0]

    def count():
        value = number("i")
        if value < 0 or value > len(raw):
            raise ValueError(f"语言表计数异常：{path.name}")
        return value

    def string():
        size = number("i")
        if size == 0:
            return ""
        data = read_exact(-size * 2 if size < 0 else size)
        return data.decode("utf-16-le" if size < 0 else "utf-8").removesuffix("\0")

    if read_exact(16) != bytes.fromhex("0e147475674a03fc4a15909dc3377f1b"):
        raise ValueError(f"不支持的语言表：{path.name}")
    version = number("B")
    if not 1 <= version <= 3:
        raise ValueError(f"不支持 locres 版本 {version}")
    offset = number("q")
    start = stream.tell()
    if not start <= offset < len(raw):
        raise ValueError(f"语言表字符串偏移异常：{path.name}")
    stream.seek(offset)
    values = []
    for _ in range(count()):
        values.append(string())
        if version >= 2:
            number("i")
    stream.seek(start)
    declared = count() if version >= 2 else None
    entries = []
    for _ in range(count()):
        if version >= 2:
            number("I")
        namespace = string()
        for _ in range(count()):
            if version >= 2:
                number("I")
            key, source_hash, index = string(), number("I"), number("i")
            if not 0 <= index < len(values):
                raise ValueError(f"语言表字符串索引异常：{path.name}")
            entries.append({"ns": namespace, "key": key, "hash": source_hash, "text": values[index]})
    if stream.tell() != offset or (declared is not None and declared != len(entries)):
        raise ValueError(f"语言表记录计数不一致：{path.name}")
    return entries


def _phrase(item: dict, *, default_scope: str = GAME_ID) -> dict:
    note = str(item.get("note", ""))
    scope = "global" if "ECDICT" in note else default_scope
    return {"term": str(item.get("en", item.get("term", ""))),
            "meaning": str(item.get("zh", item.get("translation", item.get("meaning", "")))),
            "source": note or "深海迷航 2 已有情境注释",
            "scope": scope}


def load(root: Path) -> dict:
    base = _root(root)
    mod = base / "BilingualMod"
    panel = mod / "panel"
    data = _js_data(panel / "data.js", "BILINGUAL_DATA")
    annotations = _js_data(panel / "annotations.js", "ANNOTATIONS")
    raw_lexicon = _js_data(panel / "lexicon.js", "LEXICON")
    phrases = [_phrase(item) for item in annotations.get("phrases", [])]
    reviewed = {str(value) for value in annotations.get("reviewedIds", [])}
    entries = []
    for item in data["entries"]:
        source_id = str(item["id"])
        annotation = annotations.get("entries", {}).get(source_id, {})
        if isinstance(annotation, str):
            annotation = {"note": annotation}
        note = "\n".join(dict.fromkeys(str(n) for n in (item.get("note"), annotation.get("note")) if n))
        category = "遗留与开发文本" if item.get("legacy") else CATEGORIES.get(item.get("category"), "其他文本")
        status = str(item.get("status", "游戏原译"))
        source = "本机 Game.locres · " + status
        if item.get("zh") != item.get("originalZh", item.get("zh")):
            source = "本机游戏原文 + 已有双语项目修订译文"
        entries.append({
            "key": "Game::" + str(item.get("ns", "")) + "::" + str(item["key"]),
            "namespace": str(item.get("ns", "")), "category": category,
            "en": str(item.get("en", "")), "zh": str(item.get("zh", "")),
            "speaker": "", "source": source, "note": note,
            "phrases": [_phrase(p) for p in annotation.get("phrases", [])],
            "source_id": source_id, "source_hash": item.get("hash"),
            "original_zh": item.get("originalZh", item.get("zh", "")),
            "spoiler": bool(item.get("spoiler", False)), "legacy": bool(item.get("legacy", False)),
            "reviewed": source_id in reviewed or status == "已校对",
        })
    game_count = len(entries)
    engine_files = []
    source_dir = mod / "source"
    for english_path in sorted((source_dir / "Engine").rglob("*.locres")):
        if english_path.parent.name != "en":
            continue
        chinese_path = english_path.parent.parent / "zh-Hans" / english_path.name
        if not chinese_path.is_file():
            continue
        english = _locres(english_path)
        chinese = {(v["ns"], v["key"]): v for v in _locres(chinese_path)}
        relative = english_path.relative_to(source_dir).as_posix()
        engine_files.append(relative)
        for item in english:
            target = chinese.get((item["ns"], item["key"]))
            note = "引擎或在线子系统资源；收录不代表正常游玩时会显示。"
            if target and target["hash"] != item["hash"]:
                note += "中英文原文哈希不同，配对需复核。"
            if not target:
                note += "未找到同标识的中文。"
            entries.append({
                "key": relative + "::" + item["ns"] + "::" + item["key"],
                "namespace": item["ns"],
                "category": "引擎文本" if english_path.name == "Engine.locres" else "在线与系统文本",
                "en": item["text"], "zh": target["text"] if target else "", "speaker": "",
                "source": "本机原始语言表 · " + relative, "note": note, "phrases": [],
                "source_hash": item["hash"], "spoiler": False, "legacy": False, "reviewed": False,
            })
    lexicon = {}
    for term, item in raw_lexicon.items():
        if isinstance(item, str):
            item = {"translation": item}
        domain = term in DOMAIN_TERMS or item.get("pos") == "游戏术语"
        lexicon[term] = {"meaning": str(item.get("translation", "")),
                         "source": "深海迷航 2 术语（仅限本作）" if domain else "ECDICT 通用词典",
                         "phonetic": item.get("phonetic", ""), "pos": item.get("pos", ""),
                         "scope": GAME_ID if domain else "global"}
    global_lexicon = {k: v for k, v in lexicon.items() if v["scope"] == "global"}
    version = _json(base / "version.json")
    return {"id": GAME_ID, "title": "深海迷航 2", "root": str(base), "adapter": "subnautica",
            "coverage": f"已提取本机语言表：{game_count:,} 条游戏文本及 {len(entries)-game_count:,} 条引擎/系统文本；不包含图片文字、在线新闻及玩家自定义内容。资源属于 CL-138154，当前安装 CL-{version.get('changelist', '未知')}。",
            "entries": entries, "lexicon": lexicon, "global_lexicon": global_lexicon, "phrases": phrases,
            "dictionary_license": str(mod / "source/ECDICT-LICENSE.txt"),
            "dictionary_source": str(mod / "source/ecdict.csv"), "engine_sources": engine_files,
            "capabilities": ["search", "word_notes", "phrase_notes", "enable", "disable", "launch"],
            "source_build": BUILD, "installed_build": version.get("changelist")}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _running() -> bool:
    if os.name != "nt":
        raise RuntimeError("切换与启动功能需要 Windows。")
    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Subnautica2-Win64-Test.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    return any(row and row[0].lower() == "subnautica2-win64-test.exe" for row in csv.reader(io.StringIO(result.stdout)))


def _patch_state(root: Path) -> tuple[dict, list[tuple[Path, Path]]]:
    mod = root / "BilingualMod"
    report = _json(mod / "build-report.json")
    hashes = report.get("packages", {})
    if set(hashes) != set(PACKAGE_NAMES):
        raise ValueError("补丁清单不符合本适配器支持的三个文件，未作任何修改。")
    pairs = [(mod / name, root / "Subnautica2/Content/Paks" / name) for name in PACKAGE_NAMES]
    for source, target in pairs:
        for path in (source, target):
            if not path.resolve().is_relative_to(root):
                raise ValueError("补丁路径指向游戏目录外，未作任何修改。")
        if not source.is_file() or _digest(source) != hashes[source.name]:
            raise ValueError(f"补丁源文件校验失败：{source.name}。未作任何修改。")
        if target.exists() and (not target.is_file() or _digest(target) != hashes[source.name]):
            raise ValueError(f"已有不同版本补丁：{target.name}。未覆盖或移走。")
    return hashes, pairs


def action(root: Path, action: str) -> str:
    with _ACTION_LOCK:
        return _action(root, action)


def _action(root: Path, action: str) -> str:
    base = _root(root)
    if action not in {"status", "enable", "disable", "launch"}:
        raise ValueError(f"不支持的操作：{action}")
    version = _json(base / "version.json").get("changelist")
    hashes, pairs = _patch_state(base)
    installed = sum(target.is_file() for _, target in pairs)
    if action == "status":
        state = "已启用" if installed == 3 else "已停用" if installed == 0 else "安装不完整"
        return f"{state}（{installed}/3 个补丁文件）；游戏 CL-{version}；" + ("版本匹配。" if version == BUILD else "游戏版本已变化，请停用旧补丁并重新适配。")
    if _running():
        raise RuntimeError("请先退出深海迷航 2，再切换双语模式或启动游戏。")
    # Disabling a verified old patch remains possible after a game update.
    if action != "disable" and version != BUILD:
        raise RuntimeError(f"当前游戏 CL-{version} 与补丁 CL-{BUILD} 不匹配，未启用。")
    if action == "disable":
        if installed == 0:
            return "双语模式已停用，无需更改文件。"
        disabled = base / "BilingualMod" / ("disabled-workbench-" + uuid.uuid4().hex)
        disabled.mkdir(exist_ok=False)
        moved = []
        try:
            for source, target in pairs:
                if target.is_file():
                    destination = disabled / source.name
                    # A new private directory keeps all earlier disabled copies intact.
                    if destination.exists():
                        raise FileExistsError(f"停用备份目标已存在：{destination.name}")
                    os.rename(target, destination)
                    moved.append((destination, target))
        except Exception:
            for source, target in reversed(moved):
                if source.exists() and not target.exists():
                    os.rename(source, target)
            if not any(disabled.iterdir()):
                disabled.rmdir()
            raise
        return "双语模式已停用，已恢复原游戏语言资源；未修改存档。"
    created = []
    try:
        for source, target in pairs:
            if not target.exists():
                # Exclusive create avoids clobbering files appearing after validation.
                with source.open("rb") as src, target.open("xb") as dst:
                    created.append(target)
                    shutil.copyfileobj(src, dst)
                if _digest(target) != hashes[source.name]:
                    raise ValueError(f"复制后校验失败：{target.name}")
    except Exception:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        raise
    if action == "launch":
        executable = base / "Subnautica2.exe"
        if not executable.is_file():
            raise FileNotFoundError("游戏启动程序不存在；双语补丁已启用。")
        subprocess.Popen([str(executable), "-culture=zh-Hans"], cwd=base)
        return "双语模式已启用，已启动深海迷航 2（简体中文 / English 对照）。"
    return "双语模式已启用；游戏中选择简体中文即可显示中英对照。"
