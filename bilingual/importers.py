"""Strict, key-based imports; deliberately never align translations by row order."""
import csv
import hashlib
import json
from pathlib import Path

from . import locres

MAX_BYTES = 128 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 含重复字段 {key!r}；未导入，避免静默丢失前一个值。")
        result[key] = value
    return result


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)


def checked_path(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("找不到所选文件。")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("单个导入文件不能超过 128 MB。")
    return path


def text(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是文本，不能自动转换对象或数字。")
    return value


def identifier(value):
    # Numeric IDs occur in exported game tables, but null/objects must not be
    # silently converted into apparently valid string identities.
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("key/id 必须是非空文本或整数。")
    return str(value).strip()


def phrase_list(value, row_number):
    if value == "":
        value = []  # An empty optional CSV column.
    if not isinstance(value, list):
        raise ValueError(f"第 {row_number} 条的 phrases 应为对象数组。")
    result = []
    for phrase in value:
        if not isinstance(phrase, dict):
            raise ValueError(f"第 {row_number} 条的 phrases 应为对象数组。")
        term = text(phrase.get("term"), "phrases.term").strip()
        meaning = text(phrase.get("meaning"), "phrases.meaning").strip()
        if not term or not meaning:
            raise ValueError(f"第 {row_number} 条的词组注释必须包含非空 term 和 meaning。")
        source = text(phrase.get("source"), "phrases.source").strip() or "用户导入释义"
        result.append(dict(phrase, term=term, meaning=meaning, source=source))
    return result


def package(entries, title, origin):
    if not entries:
        raise ValueError("没有找到可导入的文本条目。")
    game_id = "import-" + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
    return dict(id=game_id, title=title.strip() or "导入文本", root=origin,
                adapter="import", coverage="仅包含导入文件中的记录；可检索和注释，尚无此游戏的原生显示适配器。",
                entries=entries)


def import_file(path, title):
    path = checked_path(path)
    if path.suffix.lower() == ".json":
        content = load_json(path)
        rows = content.get("entries") if isinstance(content, dict) else content
    elif path.suffix.lower() in (".csv", ".tsv"):
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError("CSV/TSV 有重复列名，未导入。")
            rows = list(reader)
            if any(None in row for row in rows):
                raise ValueError("CSV/TSV 某行列数超过表头，未导入。")
    else:
        raise ValueError("请选择双语 JSON、CSV 或 TSV；单语言文件请用「成对导入」。")
    if not isinstance(rows, list):
        raise ValueError("需要 JSON 数组，或包含 entries 数组的对象。")
    entries, seen = [], set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 条必须是对象。")
        key = identifier(row.get("key", row.get("id")))
        namespace = text(row.get("namespace", row.get("ns", "")), "namespace")
        if not key or (namespace, key) in seen:
            raise ValueError(f"第 {i} 条缺少 key/id，或 namespace + key 重复。")
        if "en" not in row and "zh" not in row:
            raise ValueError(f"第 {i} 条缺少 en 和 zh 字段。")
        seen.add((namespace, key))
        phrases = phrase_list(row.get("phrases", []), i)
        warnings = row.get("warnings", [])
        if warnings == "":
            warnings = []
        if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
            raise ValueError(f"第 {i} 条的 warnings 应为文本数组。")
        original_source = text(row.get("source"), "source")
        source = str(path) + (f"\n原记录来源：{original_source}" if original_source else "")
        entries.append(dict(key=key, namespace=namespace, en=text(row.get("en"), "en"),
                            zh=text(row.get("zh"), "zh"), category=text(row.get("category", "导入文本"), "category"),
                            speaker=text(row.get("speaker"), "speaker"), source=source,
                            note=text(row.get("note"), "note"), phrases=phrases, warnings=warnings))
    return package(entries, title, str(path))


def language_map(path):
    path = checked_path(path)
    if path.suffix.lower() == ".locres":
        return {(e["ns"], e["key"]): (e["text"], e["hash"]) for e in locres.read(path)["entries"]}
    if path.suffix.lower() != ".json":
        raise ValueError("成对导入支持 locres v1–v3 或 JSON 键值表。")
    value = load_json(path)
    result = {}
    if isinstance(value, dict) and "entries" not in value:
        # Flat mappings are unambiguous; nested objects require explicit entry records.
        for key, translated in value.items():
            identity = ("", identifier(key))
            if identity in result:
                raise ValueError("语言表中有重复文本键。")
            result[identity] = (text(translated, key), None)
        return result
    rows = value.get("entries") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("单语言 JSON 需要键值对象或包含 key/text 的数组。")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("语言表条目缺少 key。")
        identity = (text(row.get("namespace", row.get("ns", "")), "namespace"), identifier(row.get("key")))
        if identity in result:
            raise ValueError("同一语言表中 namespace + key 重复。")
        result[identity] = (text(row.get("text", row.get("value")), "text"), row.get("hash"))
    return result


def import_pair(en_path, zh_path, title):
    en_path, zh_path = checked_path(en_path), checked_path(zh_path)
    if en_path == zh_path:
        raise ValueError("英文和中文必须选择两个不同的文件。")
    english, chinese = language_map(en_path), language_map(zh_path)
    rows = []
    for ns, key in sorted(english.keys() | chinese.keys()):
        en, en_hash = english.get((ns, key), ("", None))
        zh, zh_hash = chinese.get((ns, key), ("", None))
        warnings = []
        if en_hash is not None and zh_hash is not None and en_hash != zh_hash:
            warnings.append("两种语言的源文本哈希不一致，可能来自不同游戏版本；配对待核对。")
        rows.append(dict(key=key, namespace=ns, category="导入语言表", en=en, zh=zh,
                         source=f"{en_path} | {zh_path}", warnings=warnings))
    return package(rows, title, f"{en_path}|{zh_path}")
