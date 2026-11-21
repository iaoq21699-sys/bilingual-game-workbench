"""Read-only Electron game onboarding through explicit ASAR language data.

Container recognition is reusable; game-specific JS data is accepted only by a
registered, static-literal schema. There is no executable/package patching path.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
import json
import re

from .asar import Archive
from . import electron_red_pearls, loose_json
from ..patching import inside

CORPUS_ONLY = True
_SCHEMAS = {electron_red_pearls.PACKAGE: electron_red_pearls}
_LOCALE_DIRS = {"locales", "locale", "languages", "language", "lang", "i18n", "l10n", "translations"}
_EN = loose_json.EN
_ZH = loose_json.ZH | {"cn", "zh-hans-cn"}


def _path(root):
    for rel in ("resources/app.asar", "app.asar"):
        if (root / rel).is_file():
            return inside(root, rel)
    return None


def _metadata(archive):
    if "package.json" not in archive.entries:
        return {}
    try:
        value = archive.json("package.json", 128 * 1024)
    except (ValueError, OSError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: value[key][:200] for key in ("name", "version") if isinstance(value.get(key), str)}


def steam_appid(root):
    """Return an AppID only when the local Steam installation confirms it.

    This selects the normal store launch path, never changes Steam/app data,
    and does not inspect account or licensing fields.
    """
    root = Path(root).resolve()
    if root.parent.name.casefold() != "common" or root.parent.parent.name.casefold() != "steamapps":
        return None
    try:
        path = _path(root)
        if path is None:
            return None
        archive = Archive(path)
        if "steam_appid.txt" not in archive.entries:
            return None
        appid = archive.read("steam_appid.txt", 64).decode("ascii").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", appid):
            return None
        steamapps = root.parent.parent
        manifest = inside(steamapps, f"appmanifest_{appid}.acf")
        if not manifest.is_file() or manifest.stat().st_size > 2 * 1024 * 1024:
            return None
        content = manifest.read_text(encoding="utf-8-sig")
        ids = re.findall(r'^\s*"appid"\s+"([0-9]+)"\s*$', content, re.M)
        names = re.findall(r'^\s*"installdir"\s+"([^"\r\n]+)"\s*$', content, re.M)
        return appid if ids == [appid] and len(names) == 1 and names[0].casefold() == root.name.casefold() else None
    except (ValueError, OSError, UnicodeError):
        return None


def _locale(name):
    path = PurePosixPath(name)
    if path.suffix.lower() != ".json" or any(x.lower() in ("node_modules", "vendor") for x in path.parts):
        return None
    if path.name.lower() in {"package.json", "package-lock.json", "manifest.json", "tsconfig.json"}:
        return None
    for i, part in enumerate(path.parts):
        final = i == len(path.parts)-1
        locale = (PurePosixPath(part).stem if final else part).lower().replace("_", "-")
        if locale not in _EN | _ZH:
            continue
        if not final and not any(x.lower() in _LOCALE_DIRS for x in path.parts[:i]):
            continue
        group = list(path.parts)
        group[i] = "{locale}.json" if final else "{locale}"
        return ("en" if locale in _EN else "zh"), "/".join(group)
    return None


def _language_map(archive, path):
    data = archive.json(path)
    if not isinstance(data, dict):
        raise ValueError("ASAR 语言 JSON 不是唯一标识对象。")
    return {key: value for key, value in loose_json.leaves(data)
            if key and key[-1].lower() not in loose_json.RESERVED and value.strip()}


def _json_pairs(archive):
    groups = defaultdict(lambda: defaultdict(list))
    for path, entry in archive.entries.items():
        locale = _locale(path) if entry.offset is not None else None
        if locale:
            language, group = locale
            groups[group][language].append(path)
    result = []
    ambiguous = 0
    for group, langs in sorted(groups.items()):
        if "en" not in langs or "zh" not in langs:
            continue
        if len(langs["en"]) != 1 or len(langs["zh"]) != 1:
            ambiguous += 1
            continue
        en, zh = langs["en"][0], langs["zh"][0]
        try:
            left, right = _language_map(archive, en), _language_map(archive, zh)
            if not left.keys() & right.keys():
                ambiguous += 1
                continue
        except (ValueError, OSError, RecursionError):
            ambiguous += 1
            continue
        result.append(dict(format="json_map", english=en, chinese=zh, namespace=group))
    return result, ambiguous


def _discover(archive, metadata):
    schema = _SCHEMAS.get(metadata.get("name"))
    warnings = []
    if schema is not None:
        try:
            pairs = schema.discover(archive)
            return pairs, schema, warnings
        except ValueError as exc:
            warnings.append(str(exc))
    pairs, rejected = _json_pairs(archive)
    if rejected:
        warnings.append(f"有 {rejected} 组候选语言 JSON 缺少唯一配对标识或结构不受支持，未猜配。")
    return pairs, None, warnings


def probe(root, tools_dir=None):
    root = Path(root).resolve()
    path = _path(root)
    if path is None:
        return None
    archive = Archive(path)
    metadata = _metadata(archive)
    # An ASAR with an app manifest or normal Electron outer assets is stronger
    # than observing Chromium locale PAKs alone.
    if not metadata and not ((root / "LICENSE.electron.txt").is_file() or
                             ((root / "resources.pak").is_file() and (root / "v8_context_snapshot.bin").is_file())):
        return None
    pairs, schema, warnings = _discover(archive, metadata)
    warnings.insert(0, "语料从 ASAR 只读提取。接入后可在面板设置中启用 Electron 游戏内检索和原位双语。")
    if schema is electron_red_pearls:
        warnings.extend(["人物、地点、文档、线索和对白按稳定标识收录；没有唯一对白行标识时按共同场景合并为段落。",
                         "提示、运行时生成内容、未能配对的分支与其他界面不纳入本次覆盖；静态语料可能包含未解锁剧情。"])
    if not pairs:
        warnings.append("没有找到受支持且可可靠配对的语言数据；可导入官方语言导出或已配对文本。")
    archive.check_unchanged()
    version = metadata.get("version", "未知")
    return dict(engine="electron", root=str(root), title=root.name,
                executables=[p.name for p in sorted(root.glob("*.exe"))],
                archive=path.relative_to(root).as_posix(), package=metadata,
                recognized=True, connectable=bool(pairs), corpus_only=True,
                source_locale="en", target_locale="zh-CN", pairs=pairs, options={},
                launch_args=[], warnings=warnings,
                details=f"已识别 Electron / ASAR（版本 {version}，{len(archive.entries):,} 个文件条目）。"
                        f"发现 {len(pairs)} 组支持的中英文数据。面板加载条件可在接入后查看。")


def _json_entries(archive, pair):
    left = _language_map(archive, pair["english"])
    right = _language_map(archive, pair["chinese"])
    rows = [dict(namespace=pair["namespace"], key=json.dumps(key, ensure_ascii=False),
                 category="游戏语言表", en=left[key], zh=right[key],
                 source="app.asar: " + pair["english"] + " | " + pair["chinese"], warnings=[])
            for key in sorted(left.keys() & right.keys())]
    return rows, dict(paired_entries=len(rows), unmatched_en_fields=len(left.keys()-right.keys()),
                      unmatched_zh_fields=len(right.keys()-left.keys()))


def _pair_key(pair):
    return tuple(pair.get(key) for key in ("format", "english", "chinese", "namespace"))


def build(root, profile, workdir, tools_dir=None):
    root = Path(root).resolve()
    path = _path(root)
    if path is None or profile.get("archive") != path.relative_to(root).as_posix():
        raise ValueError("Electron 资源包位置已变化，请重新识别游戏。")
    archive = Archive(path)
    metadata = _metadata(archive)
    pairs, schema, _ = _discover(archive, metadata)
    chosen = profile.get("pairs", [])
    if not pairs or sorted(map(_pair_key, pairs), key=str) != sorted(map(_pair_key, chosen), key=str):
        raise ValueError("版本或中英文语言资源已变化，未读取未识别的模块；请重新从目录接入。")
    entries, pair_reports = [], []
    totals = Counter()
    for pair in pairs:
        if pair["format"] == "json_map":
            rows, report = _json_entries(archive, pair)
        elif schema is not None:
            rows, report = schema.build_pair(archive, pair)
        else:
            raise ValueError("此 Electron 数据格式尚不支持。")
        if not rows:
            raise ValueError("已识别的语言表没有可靠中英文配对。")
        entries.extend(rows)
        totals.update(report)
        pair_reports.append(dict(format=pair["format"], entries=len(rows), **report))
    archive.check_unchanged()
    if len({(row["namespace"], row["key"]) for row in entries}) != len(entries):
        raise ValueError("Electron 语料稳定标识重复，旧索引未更改。")
    coverage = f"已从 ASAR 只读收录 {len(entries):,} 条中英对照。"
    if totals.get("dialogue_lines"):
        coverage += f"覆盖 {totals['dialogue_lines']} 个对白文本字段（含整段场景）；无独立行标识时不逐行猜配。"
    coverage += f"未可靠对应的文字字段：英文 {totals['unmatched_en_fields']}、中文 {totals['unmatched_zh_fields']}。"
    return dict(entries=entries, files=[], fingerprints=[],
                report=dict(coverage=coverage, corpus_only=True, pairs=pair_reports,
                            categories=dict(Counter(row["category"] for row in entries)),
                            totals=dict(totals), archive=path.relative_to(root).as_posix(),
                            package=metadata, archive_signature=list(archive.signature)))
