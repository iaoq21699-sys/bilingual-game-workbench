"""Configurable language-map integration for games reading loose JSON files."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
from collections import Counter

from ..importers import load_json
from ..patching import digest, inside

EN = {"en", "eng", "en-us", "en-gb", "english"}
ZH = {"zh", "zhs", "zh-cn", "zh-hans", "schinese", "chinese", "simplifiedchinese"}
PARAMS = re.compile(r"\{[^{}]+\}|%(?:\d+\$)?[-+0-9.]*[sdfiu]")
RESERVED = {"locale", "locale_code", "language_code", "font", "font_path", "fontfamily", "version", "$schema"}


def _locale(path):
    parts = path.as_posix().split("/")
    for i, raw in enumerate(parts):
        name = (Path(raw).stem if i == len(parts)-1 else raw).lower().replace("_", "-")
        if name in EN | ZH:
            group = list(parts)
            group[i] = "{locale}.json" if i == len(parts)-1 else "{locale}"
            return "en" if name in EN else "zh", "/".join(group), raw
    return None


def probe(root, tools_dir=None):
    root = Path(root).resolve()
    maps = {}
    scanned = 0
    for current, folders, files in os.walk(root):
        relative = Path(current).relative_to(root)
        folders[:] = sorted(d for d in folders if not d.startswith(".") and d.lower() not in {"node_modules", "saved", "logs", "cache", "backups"})
        if len(relative.parts) > 6:
            folders[:] = []
            continue
        for name in files:
            scanned += 1
            if scanned > 12000:
                break
            if name.lower().endswith(".json"):
                path = relative / name
                locale = _locale(path)
                if locale:
                    language, group, _ = locale
                    maps.setdefault(group, {}).setdefault(language, path.as_posix())
        if scanned > 12000:
            break
    pairs = [dict(english=v["en"], chinese=v["zh"], target=v["zh"], namespace=k)
             for k, v in sorted(maps.items()) if "en" in v and "zh" in v]
    if not pairs:
        return None
    return dict(engine="loose_json", root=str(root), title=root.name,
                executables=[p.name for p in sorted(root.glob("*.exe"))], pairs=pairs,
                source_locale="en", target_locale="zh", options={}, launch_args=[],
                details=f"找到 {len(pairs)} 对松散 JSON 语言文件；将备份并替换中文语言文件。",
                warnings=["需确认该游戏实际读取这些语言文件；启用后在游戏语言设置中选择中文。",
                          "本格式支持纯字符串键值表及嵌套对象；不支持按数组行号猜配。"])


def leaves(value, prefix=()):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, list):
                raise ValueError("语言表包含数组，需为其稳定 ID 配置专用格式，未按行号拼接。")
            yield from leaves(item, prefix + (key,))
    elif value is None or isinstance(value, (int, float, bool)):
        return
    else:
        raise ValueError("语言表包含不支持的数据。")


def combine(en, zh, options):
    if not en.strip() or not zh.strip() or en.strip() == zh.strip():
        return zh
    if Counter(PARAMS.findall(en)) != Counter(PARAMS.findall(zh)):
        return zh
    order = options.get("order", "zh-en")
    layout = options.get("layout", "auto")
    separator = " / " if layout == "inline" or (layout == "auto" and len(en) + len(zh) < 45 and "\n" not in en + zh) else "\n"
    return separator.join((en, zh) if order == "en-zh" else (zh, en))


def build(root, profile, workdir, tools_dir=None):
    root, workdir = Path(root).resolve(), Path(workdir).resolve()
    entries, files, fingerprints, missing, skipped, changed = [], [], [], 0, 0, 0
    source_hashes = {}
    seen_targets = set()
    for index, pair in enumerate(profile["pairs"]):
        english_path, chinese_path = inside(root, pair["english"]), inside(root, pair["chinese"])
        target = inside(root, pair.get("target", pair["chinese"]))
        if target != chinese_path:
            raise ValueError("松散 JSON 模式只替换所选中文表；其他输出位置需要单独适配。")
        if target in seen_targets:
            raise ValueError("语言配置重复指向同一个中文文件。")
        seen_targets.add(target)
        for path in (english_path, chinese_path):
            current_hash = digest(path)
            if path in source_hashes and source_hashes[path] != current_hash:
                raise ValueError("生成期间游戏语言文件发生变化，请等待更新完成后重新接入。")
            source_hashes.setdefault(path, current_hash)
        en, zh = load_json(english_path), load_json(chinese_path)
        if not isinstance(en, dict) or not isinstance(zh, dict):
            raise ValueError("松散 JSON 需要对象结构，非字符串表未修改。")
        eng, chn = dict(leaves(en)), dict(leaves(zh))
        output = copy.deepcopy(zh)
        namespace = pair.get("namespace", pair["english"])
        for key in sorted(eng.keys() | chn.keys()):
            e, z = eng.get(key, ""), chn.get(key, "")
            warnings = []
            if not e or not z:
                missing += 1
            text = combine(e, z, profile.get("options", {}))
            if key[-1].lower() in RESERVED or "/".join(key) in profile.get("options", {}).get("excluded_keys", []):
                text = z
                warnings.append("配置或字体字段，保留原值。")
            if e and z and Counter(PARAMS.findall(e)) != Counter(PARAMS.findall(z)):
                skipped += 1
                warnings.append("中英格式参数不一致，游戏中保留原中文。")
            if key in chn and text != z:
                parent = output
                for part in key[:-1]:
                    parent = parent[part]
                parent[key[-1]] = text
                changed += 1
            entries.append(dict(key=json.dumps(key, ensure_ascii=False), namespace=namespace,
                                category="游戏文本", en=e, zh=z, source=f"{pair['english']} | {pair['chinese']}", warnings=warnings))
        destination = workdir / f"language-{index}.json"
        destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(dict(source=str(destination), target=target.relative_to(root).as_posix()))
    # Bind the generated bytes to exactly the source version read above. A game
    # update during generation must never be accepted as the output's source.
    for path, original_hash in source_hashes.items():
        if digest(path) != original_hash:
            raise ValueError("生成期间游戏语言文件发生变化，请等待更新完成后重新接入。")
        fingerprints.append(dict(path=path.relative_to(root).as_posix(), sha256=original_hash))
    return dict(entries=entries, files=files, fingerprints=fingerprints,
                report=dict(changed=changed, missing=missing, skipped=skipped,
                            coverage=f"{changed} 条已生成双语；{skipped} 条参数异常保留原文；缺语言 {missing} 条。"))
