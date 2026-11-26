"""Configurable Godot JSON localization overlay builder; staging only.

Probe returns PCK resource pairs. A profile is the probe dictionary, with optional
options changes. Known STS2 profiles use its native data-only mod loader; other
games need an explicitly configured loader and resource prefix. No DLL injection,
original PCK rewriting, game installation, or game launch occurs in this module.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import uuid

from .pck import Archive, resource_path, write as write_pck


SOURCE_LOCALES = ("eng", "en", "en-US", "en_US", "english", "English")
TARGET_LOCALES = ("zhs", "zh-Hans", "zh-CN", "zh_CN", "schinese", "zh", "ChineseSimplified")
MOD_ID = "BilingualWorkbench"
FORMAT_ARGS = re.compile(r"\{\s*([A-Za-z_][A-Za-z_0-9.]*|\d+)(?=[:},])")
BBCODE = re.compile(r"\[(/?[A-Za-z_][A-Za-z_0-9]*)(?:=[^\]]*)?\]")
TABLE_LABELS = {
    "cards": "卡牌", "relics": "遗物", "powers": "状态与能力", "events": "事件与对话",
    "ancients": "远古之民", "monsters": "怪物", "characters": "角色", "potions": "药水",
    "main_menu_ui": "主菜单", "settings_ui": "设置", "epochs": "历史记录",
    "achievements": "成就", "credits": "制作人员", "ftues": "教程",
}


def _separator(options, table, key, english, chinese):
    """Respect explicit layout; automatic short labels stay on one line."""
    layout = options.get("layout")
    if layout is None:
        return options.get("separator", "\n")  # Older saved profiles.
    if layout == "inline":
        return " / "
    if layout == "newline":
        return "\n"
    short_label = (table.endswith("_ui") or table in {"menu", "map", "acts", "card_keywords", "card_selection"}
                   or key.rsplit(".", 1)[-1].lower() in {"title", "name", "header"})
    en = re.sub(r"\[[^\]]*\]", "", english)
    zh = re.sub(r"\[[^\]]*\]", "", chinese)
    if short_label and "\n" not in en + zh and len(en) <= 48 and len(zh) <= 24 and len(en) + len(zh) <= 64:
        return " / "
    return "\n"


def _root(value, *, optional=False):
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("所选游戏目录不存在。")
    if any(root.glob("*.pck")):
        return root
    candidates = [p for p in root.iterdir() if p.is_dir() and any(p.glob("*.pck"))]
    if (root / "playtest").is_dir():
        candidates += [p for p in (root / "playtest").iterdir() if p.is_dir() and any(p.glob("*.pck"))]
    if len(candidates) == 1:
        return candidates[0].resolve()
    if optional and not candidates:
        return None
    raise ValueError("未找到唯一的独立 Godot PCK；请选择包含游戏 EXE 和 PCK 的目录。")


def _inside(root: Path, relative: str) -> Path:
    relative = resource_path(relative)
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("配置路径越过游戏目录，未继续。")
    return path


def _load_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"语言 JSON 含重复键：{key}")
            result[key] = value
        return result
    result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
    if not isinstance(result, dict) or any(not isinstance(v, str) for v in result.values()):
        raise ValueError("当前 Godot 适配器要求 JSON 字符串键值表，不支持此资源的嵌套/非文本结构。")
    return result


def probe(root, tools_dir=None):
    root = _root(root, optional=True)
    if root is None:
        return None
    packs = []
    failures = []
    for path in sorted(root.glob("*.pck")):
        try:
            packs.append((path, Archive(path)))
        except (OSError, ValueError) as exc:
            failures.append(f"{path.name}：{exc}")
    paths = {name for _, archive in packs for name in archive.entries if name.endswith(".json")}
    source = next((locale for locale in SOURCE_LOCALES if any(f"/{locale}/" in "/" + n for n in paths)), "eng")
    target = next((locale for locale in TARGET_LOCALES if any(f"/{locale}/" in "/" + n for n in paths)), "zhs")
    pairs = []
    for path, archive in packs:
        groups = {}
        for name in archive.entries:
            if not name.endswith(".json"):
                continue
            segments = name.split("/")
            for index, segment in enumerate(segments[:-1]):
                if segment not in (source, target):
                    continue
                table = "/".join(segments[:index] + ["{locale}"] + segments[index+1:])
                group = groups.setdefault(table, {"pack": path.name, "table": table, "source": None, "target": None})
                group["source" if segment == source else "target"] = name
                break
        pairs += [groups[key] for key in sorted(groups)]
    known = (root / "SlayTheSpire2.exe").is_file() and (root / "data_sts2_windows_x86_64/sts2.dll").is_file()
    warnings = failures[:]
    if not pairs:
        warnings.append("没有发现中英语言 JSON 配对；可在配置中指定 PCK 与资源路径。")
    if known:
        warnings.append("首次启用模组可能需要游戏内确认；游戏区分模组与原版存档，显示效果需进游戏检查。")
    else:
        warnings.append("此游戏的覆盖包加载方式未知；必须配置已存在的模组加载路径，不能仅放入 PCK 就保证生效。")
    release = {}
    if (root / "release_info.json").is_file():
        release = json.loads((root / "release_info.json").read_text(encoding="utf-8-sig"))
    return {
        "engine": "godot", "root": str(root),
        "title": "杀戮尖塔 2" if known else root.name,
        "executables": [p.name for p in sorted(root.glob("*.exe")) if not any(t in p.name.lower() for t in ("crash", "unins"))],
        "pairs": pairs, "source_locale": source, "target_locale": target,
        "options": {
            "loader": "sts2" if known else "unconfigured",
            "mod_id": MOD_ID,
            "mod_directory": f"mods/{MOD_ID}",
            "resource_prefix": MOD_ID if known else "",
            "write_locales": [source, target],
            "separator": "\n", "layout": "auto", "order": "zh-en",
            "on_mismatch": "preserve",
        },
        "details": {"packs": [{"path": p.name, "format": a.version, "engine_version": a.engine_version,
                                "resources": len(a.entries)} for p, a in packs],
                    "release": release, "pair_count": len(pairs), "native_loader_known": known,
                    "format_reference": "https://github.com/godotengine/godot/blob/4.5/core/io/file_access_pack.cpp",
                    "loader_reference": "https://github.com/Alchyr/ModTemplate-StS2/wiki/Modding-Basics"},
        "warnings": warnings,
    }


def _fingerprint(root, path):
    stat = path.stat()
    result = {"path": path.relative_to(root).as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if stat.st_size <= 16 * 1024 * 1024:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def build(root, profile, workdir, tools_dir=None):
    root = _root(root)
    options = profile.get("options", {})
    loader = options.get("loader", "unconfigured")
    if loader not in {"sts2", "pck-overlay"}:
        raise ValueError("尚未配置该游戏的 PCK 模组加载方式，无法生成可生效的双语覆盖包。")
    if loader == "sts2" and not (root / "SlayTheSpire2.exe").is_file():
        raise ValueError("STS2 模组加载配置只能用于已识别的杀戮尖塔 2 目录。")
    mod_id = options.get("mod_id", MOD_ID)
    if not isinstance(mod_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", mod_id):
        raise ValueError("mod_id 需以英文字母开头，且仅含字母、数字、下划线。")
    mod_dir = resource_path(options.get("mod_directory", f"mods/{mod_id}"))
    _inside(root, mod_dir)
    prefix = options.get("resource_prefix", mod_id if loader == "sts2" else "").strip("/")
    if prefix:
        prefix = resource_path(prefix)
    source_locale = profile.get("source_locale", "eng")
    target_locale = profile.get("target_locale", "zhs")
    if source_locale == target_locale:
        raise ValueError("两种语言不能相同。")
    write_locales = options.get("write_locales", [target_locale])
    if not isinstance(write_locales, list) or not write_locales or any(x not in (source_locale, target_locale) for x in write_locales):
        raise ValueError("write_locales 只能包含已选择的中英文语言。")
    if options.get("on_mismatch", "preserve") not in {"preserve", "combine"}:
        raise ValueError("on_mismatch 必须是 preserve 或 combine。")
    if options.get("layout") not in {None, "auto", "newline", "inline"}:
        raise ValueError("layout 必须是 auto、newline 或 inline。")
    separator = options.get("separator", "\n")
    if not isinstance(separator, str) or len(separator) > 16:
        raise ValueError("双语分隔符必须是最多 16 个字符的文本。")
    pairs = profile.get("pairs", [])
    if not pairs:
        raise ValueError("没有配置需要合并的语言资源。")
    work = Path(workdir).resolve()
    # Staging in a dedicated subfolder avoids replacing a previous build.
    stage = work / ("godot-" + uuid.uuid4().hex)
    stage.mkdir(parents=True, exist_ok=False)
    archives = {}
    fingerprints = []
    entries, resources, issues = [], {}, []
    stats = Counter()
    identities = set()
    for pair in pairs:
        pack = resource_path(pair["pack"])
        if pack not in archives:
            pack_path = _inside(root, pack)
            archives[pack] = Archive(pack_path)
            fingerprints.append(_fingerprint(root, pack_path))
        archive = archives[pack]
        source_path, target_path = pair.get("source"), pair.get("target")
        english = _load_json(archive.read(source_path)) if source_path else {}
        chinese = _load_json(archive.read(target_path)) if target_path else {}
        template = pair.get("table")
        if not isinstance(template, str) or template.count("{locale}") != 1:
            raise ValueError("资源 pair.table 必须包含一个 {locale} 目录占位符。")
        table = template.replace("{locale}/", "")
        table_stem = Path(table).stem
        output = {locale: {} for locale in write_locales}
        for key in sorted(english.keys() | chinese.keys()):
            identity = f"{pack}!{table}#{key}"
            if identity in identities:
                raise ValueError(f"重复资源标识：{identity}")
            identities.add(identity)
            en, zh = english.get(key, ""), chinese.get(key, "")
            warnings = []
            mismatch = False
            if en and zh:
                if Counter(FORMAT_ARGS.findall(en)) != Counter(FORMAT_ARGS.findall(zh)):
                    warnings.append("两种语言的格式参数或次数不同，保留原文并标记核对。")
                    mismatch = True
                if Counter(BBCODE.findall(en)) != Counter(BBCODE.findall(zh)):
                    warnings.append("两种语言的富文本标签不同，保留原文并标记核对。")
                    mismatch = True
            if not en or not zh:
                warnings.append("缺少一种语言，保留已有原文，未虚构翻译。")
                stats["missing_language"] += 1
            entries.append({"key": identity, "namespace": table, "category": TABLE_LABELS.get(table_stem, "界面与文本"),
                            "en": en, "zh": zh, "speaker": "", "source": f"{pack}!{template}#{key}",
                            "note": "", "phrases": [], "warnings": warnings})
            if mismatch:
                stats["mismatched"] += 1
                issues.append({"table": table, "key": key, "warnings": warnings})
            if en and zh and en != zh and (not mismatch or options.get("on_mismatch") == "combine"):
                selected_separator = _separator(options, table_stem, key, en, zh)
                combined = selected_separator.join((en, zh) if options.get("order") == "en-zh" else (zh, en))
                stats["inline" if selected_separator == " / " else "newline"] += 1
                stats["bilingual"] += 1
            else:
                combined = None
                stats["unchanged"] += 1
            for locale in write_locales:
                original = english if locale == source_locale else chinese
                if key in original:
                    output[locale][key] = combined if combined is not None else original[key]
                elif combined is not None:
                    output[locale][key] = combined
        for locale, values in output.items():
            if not values:
                continue
            destination = template.replace("{locale}", locale)
            if prefix:
                destination = prefix + "/" + destination
            destination = resource_path(destination)
            if destination in resources:
                raise ValueError(f"多个源资源映射到同一覆盖目标：{destination}")
            resources[destination] = (json.dumps(values, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        stats["tables"] += 1
    if not entries or not stats["bilingual"]:
        raise ValueError("未找到可安全合并的双语文本，未生成覆盖包。")
    overlay = stage / f"{mod_id}.pck"
    write_pck(overlay, resources)
    files = [{"source": str(overlay), "target": f"{mod_dir}/{overlay.name}"}]
    if loader == "sts2":
        manifest = {"id": mod_id, "name": "中英双语 / Bilingual Workbench", "author": "Local Bilingual Workbench",
                    "description": "Locally generated bilingual localization. No gameplay code changes.",
                    "version": "1.0.0", "has_pck": True, "has_dll": False, "dependencies": [],
                    "affects_gameplay": False}
        path = stage / f"{mod_id}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append({"source": str(path), "target": f"{mod_dir}/{path.name}"})
    for relative in ("release_info.json", "data_sts2_windows_x86_64/sts2.dll"):
        if (root / relative).is_file():
            fingerprints.append(_fingerprint(root, root / relative))
    # Recheck the inputs after reading so a game update cannot silently race a build.
    for fingerprint in fingerprints:
        current = _fingerprint(root, root / fingerprint["path"])
        if current != fingerprint:
            raise ValueError("构建期间游戏文件变化，未提交覆盖包。请重新扫描。")
    report = {"engine": "godot", "entry_count": len(entries), **stats,
              "resource_files": len(resources), "pck_verified": True,
              "runtime_verified": False, "loader": loader, "requires_dll": False,
              "layout": options.get("layout", "custom-separator"), "order": options.get("order", "zh-en"),
              "source_locale": source_locale, "target_locale": target_locale,
              "issues": issues, "coverage": "覆盖配置中的语言 JSON；不包含贴图文字或运行时生成文本。",
              "warnings": ["双语文本更长，个别界面可能需要游戏专用布局调整。",
                           "格式参数或富文本不一致的条目默认只收录，不自动改写。",
                           "覆盖包已回读验证，尚未确认游戏内加载和显示效果。"]}
    (stage / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"entries": entries, "files": files, "fingerprints": fingerprints, "report": report}
