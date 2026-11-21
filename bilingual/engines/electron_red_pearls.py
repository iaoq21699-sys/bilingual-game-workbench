"""Data schema for The Red Pearls of Borneo's multilingual resource modules.

Only literal localization fields are read. Gameplay expressions, asset
references and application code are not interpreted or changed. Array rows
without stable identities are never guessed by row number.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re

from .jsdata import parse_data_module, parse_literal_object, OPAQUE

PACKAGE = "the-red-pearls-of-borneo"
PAGE = "dist/index.html"
TEXT_ELEMENTS = (
    "#dialogue-text,#dialogue-speaker-name,#current-location,#current-time-display,#timeline-guest-name,"
    "#timeline-item-name,#annotation-original-text,#cw-text,#diff-desc,.dialogue-text,.speaker-name,"
    ".transcript-text,.transcript-speaker,.scene-title,.item-front-name,.item-name,.item-desc,"
    ".doc-title,.doc-preview,.letter-body,.intro-title,.intro-text,.clue-text,.clue-narration-line,"
    ".clue-highlight,.discovery-title,.name,.room-label,.room-meta-info,.date-label,.address,.title-text,.open-instruction,"
    ".modal-section-label,.guest-track-short,.timeline-header span"
)
_PREFIX = "dist/assets/"
_TEXT = frozenset(k.lower() for k in (
    "name", "firstName", "lastName", "strangername", "description", "short",
    "clue", "text", "title", "fullText", "preview", "header"))
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\u1100-\u11ff\uac00-\ud7af]")
_LATIN = re.compile(r"[A-Za-z]")


def _literal_texts(value, fields=_TEXT):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in fields and isinstance(child, str) and child.strip():
                yield child
            elif isinstance(child, (dict, list)):
                yield from _literal_texts(child, fields)
    elif isinstance(value, list):
        for child in value:
            yield from _literal_texts(child, fields)


def _module(archive, path):
    # A narrow filename allowlist avoids reading executable entry points or
    # unrelated application bundles, even with an imported profile.
    if not re.fullmatch(r"dist/assets/(?:data|dialogue)(?:_[a-z]+)?-[A-Za-z0-9_-]+\.js", path):
        raise ValueError("没有指定已识别的语言数据模块。")
    return parse_data_module(archive.read(path))


def _dialogue_language(roots):
    values = list(_literal_texts(roots, frozenset({"text", "speaker", "title"})))
    han = sum(len(_HAN.findall(text)) for text in values)
    latin = sum(len(_LATIN.findall(text)) for text in values)
    kana = sum(len(_KANA.findall(text)) for text in values)
    hangul = sum(len(_HANGUL.findall(text)) for text in values)
    if han > 100 and not kana and not hangul:
        return "zh"
    if latin > 100 and not han and not kana and not hangul:
        return "en"
    return None


def discover(archive):
    def unique(pattern):
        matches = [path for path, entry in archive.entries.items()
                   if entry.offset is not None and re.fullmatch(pattern, path)]
        if len(matches) != 1:
            raise ValueError("主要中英文数据包无法唯一识别，请重新检查游戏版本。")
        return matches[0]

    english = unique(r"dist/assets/data-[A-Za-z0-9_-]+\.js")
    chinese = unique(r"dist/assets/data_(?:cn|zh)-[A-Za-z0-9_-]+\.js")
    candidates = {"en": [], "zh": []}
    # Some language chunks have no locale suffix. For this known data schema,
    # classify only their static dialogue text, not code or resource filenames.
    for path, entry in archive.entries.items():
        if entry.offset is None or not re.fullmatch(r"dist/assets/dialogue(?:_(?:en|cn|zh))?-[A-Za-z0-9_-]+\.js", path):
            continue
        roots, _ = _module(archive, path)
        locale = _dialogue_language(roots)
        if locale:
            candidates[locale].append(path)
    if any(len(values) != 1 for values in candidates.values()):
        raise ValueError("中英文对白包无法唯一识别；不会猜测其他拉丁语种。")
    return [dict(format="red_pearls_data", english=english, chinese=chinese,
                 namespace="main-data"),
            dict(format="red_pearls_dialogue", english=candidates["en"][0],
                 chinese=candidates["zh"][0], namespace="dialogue")]


def _ids(value):
    if not isinstance(value, list) or not value:
        return None
    result = {}
    for row in value:
        if not isinstance(row, dict) or type(row.get("id")) not in (str, int) or not str(row["id"]).strip():
            return None
        identity = (type(row["id"]).__name__, row["id"])
        if identity in result:
            return None
        result[identity] = row
    return result


def _root_identity(root):
    if isinstance(root, dict) and root:
        return ("keys", frozenset(root))
    ids = _ids(root)
    return ("ids", frozenset(ids)) if ids else None


def _role(root):
    rows = root if isinstance(root, list) else root.values()
    fields = {key for row in rows if isinstance(row, dict) for key in row}
    if "firstName" in fields:
        return "people", "人物与身份"
    if {"short", "x", "y"} <= fields:
        return "places", "地点与地图"
    if {"preview", "fullText"} <= fields:
        return "documents", "文档与记录"
    if {"ownerId", "description"} <= fields:
        return "items", "物品与说明"
    if "clue" in fields:
        return "scenes", "场景与线索"
    if {"title", "text"} <= fields:
        return "reference", "参考资料"
    return None


def _path_key(path):
    return json.dumps(path, ensure_ascii=False, separators=(",", ":"))


class _Pairs:
    def __init__(self, archive, pair):
        self.source = "app.asar: " + pair["english"] + " | " + pair["chinese"]
        self.entries = []
        self.stats = Counter()

    def add(self, namespace, category, path, en, zh, *, speaker="", warnings=()):
        if not isinstance(en, str) or not isinstance(zh, str):
            return False
        if not en.strip() or not zh.strip() or not _LATIN.search(en):
            return False
        if len(en) > 128_000 or len(zh) > 128_000:
            self.stats["oversized_text"] += 1
            return False
        self.entries.append(dict(namespace=namespace, key=_path_key(path),
                                 category=category, en=en, zh=zh, speaker=speaker,
                                 source=self.source, warnings=list(warnings)))
        self.stats["paired_entries"] += 1
        return True

    def omit(self, en, zh):
        self.stats["unmatched_en_fields"] += sum(1 for _ in _literal_texts(en))
        self.stats["unmatched_zh_fields"] += sum(1 for _ in _literal_texts(zh))

    def walk(self, en, zh, role, category, path=()):
        if isinstance(en, dict) and isinstance(zh, dict):
            for key in sorted(en.keys() | zh.keys()):
                a, b = en.get(key, OPAQUE), zh.get(key, OPAQUE)
                if key.lower() in _TEXT and (isinstance(a, str) or isinstance(b, str)):
                    if not self.add(role, category, path + (("field", key),), a, b):
                        self.stats["unmatched_en_fields"] += int(isinstance(a, str) and bool(a.strip()))
                        self.stats["unmatched_zh_fields"] += int(isinstance(b, str) and bool(b.strip()))
                elif isinstance(a, (dict, list)) and isinstance(b, (dict, list)):
                    self.walk(a, b, role, category, path + (("key", key),))
                elif isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
                    self.omit(a, b)
        elif isinstance(en, list) and isinstance(zh, list):
            left, right = _ids(en), _ids(zh)
            if left is None or right is None:
                # Non-text metadata arrays are common; skip unkeyed arrays rather
                # than pretending their indexes are cross-language identities.
                self.omit(en, zh)
                return
            for identity in sorted(left.keys() | right.keys(), key=str):
                if identity in left and identity in right:
                    self.walk(left[identity], right[identity], role, category,
                              path + (("id", *identity),))
                else:
                    self.omit(left.get(identity), right.get(identity))
        else:
            self.omit(en, zh)


def _main(archive, pair):
    english, eo = _module(archive, pair["english"])
    chinese, zo = _module(archive, pair["chinese"])
    result = _Pairs(archive, pair)
    result.stats.update(opaque_en=eo, opaque_zh=zo)
    used = set()
    roles = set()
    for root in english:
        identity = _root_identity(root)
        matches = [i for i, other in enumerate(chinese) if identity is not None and
                   identity == _root_identity(other)]
        role = _role(root)
        if len(matches) != 1 or matches[0] in used or role is None or role[0] in roles:
            result.omit(root, None)
            result.stats["unmatched_en_roots"] += 1
            continue
        roles.add(role[0])
        used.add(matches[0])
        result.stats["matched_roots"] += 1
        result.walk(root, chinese[matches[0]], *role)
    for i, root in enumerate(chinese):
        if i not in used:
            result.omit(None, root)
            result.stats["unmatched_zh_roots"] += 1
    if not result.entries or not result.stats["matched_roots"]:
        raise ValueError("主要语言数据结构已变化，未生成不可靠的配对。")
    return result


def _anchor(row):
    if not isinstance(row, dict) or not isinstance(row.get("text"), str):
        return None
    # Stable non-language metadata such as portrait/action identifiers. Opaque
    # expressions are never compared as values or interpreted.
    return (tuple(sorted(row)), tuple((key, type(value).__name__, value)
            for key, value in sorted(row.items()) if key not in {"speaker", "text", "title"}
            and type(value) in (str, int, float, bool, type(None))))


def _line_text(row):
    speaker = row.get("speaker", "")
    return (speaker + ": " if isinstance(speaker, str) and speaker.strip() else "") + row["text"]


def _dialogue_lines(result, left, right, path):
    if not left and not right:
        return
    if not isinstance(left, list) or not isinstance(right, list):
        result.omit(left, right)
        return
    en, zh = [_anchor(r) for r in left], [_anchor(r) for r in right]
    if None in en or None in zh or Counter(en) != Counter(zh):
        result.omit(left, right)
        result.stats["skipped_line_groups"] += 1
        return
    if len(set(en)) == len(en):
        # Unique anchor within the same stable scene key (or a singleton).
        mapped = dict(zip(zh, right))
        for anchor, line in zip(en, left):
            translated = mapped[anchor]
            identity = hashlib.sha256(_path_key(anchor).encode("utf-8")).hexdigest()[:16]
            if result.add("dialogue", "剧情对白", path + (("line", identity),),
                          line["text"], translated["text"],
                          speaker=line.get("speaker") if isinstance(line.get("speaker"), str) else ""):
                result.stats["dialogue_lines"] += 1
                result.stats["dialogue_individual_lines"] += 1
        return
    if en != zh:
        result.omit(left, right)
        result.stats["skipped_line_groups"] += 1
        return
    # Repeated anchors cannot identify individual translated lines. Preserve
    # both original scene sequences as one bilingual passage, not row pairing.
    if result.add("dialogue", "剧情对白·场景段落", path + (("passage", "lines"),),
                  "\n".join(_line_text(row) for row in left),
                  "\n".join(_line_text(row) for row in right),
                  warnings=("共同场景标识配对；句内无独立稳定行 ID，保留各语言整段顺序，未按行号猜配。",)):
        result.stats["dialogue_grouped_scenes"] += 1
        result.stats["dialogue_lines"] += len(left)
        result.stats["dialogue_grouped_lines"] += len(left)


def _dialogue(archive, pair):
    english, eo = _module(archive, pair["english"])
    chinese, zo = _module(archive, pair["chinese"])
    if len(english) != 1 or len(chinese) != 1 or not isinstance(english[0], dict) or not isinstance(chinese[0], dict):
        raise ValueError("对白语言表不再是可确认的场景标识结构。")
    if _dialogue_language(english) != "en" or _dialogue_language(chinese) != "zh":
        raise ValueError("对白文件的语言证据不匹配。")
    result = _Pairs(archive, pair)
    result.stats.update(opaque_en=eo, opaque_zh=zo)

    def walk(left, right, path):
        for key in sorted(left.keys() | right.keys()):
            a, b = left.get(key, OPAQUE), right.get(key, OPAQUE)
            here = path + (("key", key),)
            if key == "lines":
                _dialogue_lines(result, a, b, here)
            elif key == "title" and (isinstance(a, str) or isinstance(b, str)):
                if not result.add("dialogue", "对白标题", here, a, b):
                    result.stats["unmatched_en_fields"] += int(isinstance(a, str))
                    result.stats["unmatched_zh_fields"] += int(isinstance(b, str))
            elif isinstance(a, dict) and isinstance(b, dict):
                walk(a, b, here)
            elif isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
                result.omit(a, b)

    left, right = english[0], chinese[0]
    result.stats["matched_scenes"] = len(left.keys() & right.keys())
    walk(left, right, ())
    if not result.entries:
        raise ValueError("没有找到可按场景标识配对的对白。")
    return result


def build_pair(archive, pair):
    if pair["format"] == "red_pearls_data":
        result = _main(archive, pair)
    elif pair["format"] == "red_pearls_dialogue":
        result = _dialogue(archive, pair)
    else:
        raise ValueError("未知的 Electron 语言数据结构。")
    return result.entries, dict(result.stats)


def _ui_source(archive):
    document = archive.read(PAGE, 256 * 1024).decode("utf-8")
    scripts = re.findall(r'<script\b[^>]*\bsrc="\./(assets/index-[A-Za-z0-9_-]+\.js)"', document)
    if len(scripts) != 1:
        raise ValueError("未能唯一识别婆罗洲的界面词表所在脚本。")
    return archive.read("dist/" + scripts[0], 512 * 1024).decode("utf-8")


def _ui_tables(source):
    tables = {}
    starts = {}
    for match in re.finditer(r"(?<![\w$])(en|cn):\s*(?=\{\s*settings_tooltip:)", source):
        locale = match.group(1)
        if locale in tables:
            raise ValueError("婆罗洲界面词表重复。")
        tables[locale] = parse_literal_object(source[match.end():match.end() + 50_000])
        starts[locale] = match.end()
    if set(tables) != {"en", "cn"}:
        raise ValueError("婆罗洲界面词表的语言标记已变化。")
    return tables, starts


def ui_labels(archive):
    """Read two anchored UI dictionaries referenced by the packaged HTML."""
    try:
        tables, _ = _ui_tables(_ui_source(archive))
    except ValueError:
        return {}
    left, right = tables["en"], tables["cn"]
    return {key: (left[key], right[key]) for key in left.keys() & right.keys()
            if isinstance(left[key], str) and isinstance(right[key], str)}


def reading_data(archive, pairs, plain):
    from .electron_red_pearls_reading import build
    return build(archive, pairs, plain)
