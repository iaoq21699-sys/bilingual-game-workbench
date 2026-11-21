"""Borneo's visible text and the stable locations used to match it at runtime."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import re

from . import electron_red_pearls as game
from .jsdata import parse_literal_object


_DIRECTIVE = re.compile(r"^@(p\d+|unknown|note):\s*", re.I)
_CENSORED = re.compile(r"<c>[^<>]*</c>", re.I)
_REFERENCE = re.compile(r"&(p\d+)(?:_(firstname|lastname|title))?(?!\w)|&([a-z][a-z0-9]+)\b", re.I)
_MONTHS = {name: index for index, name in enumerate(
    "January February March April May June July August September October November December".split(), 1)}
_PERIODS = {"早": "Morning", "午": "Afternoon", "晚": "Evening"}
# The translated queues and STUDY_11's three branch predicates were checked
# against these exact modules. Other releases still use unique metadata IDs.
_REVIEWED_DIALOGUE = (
    "0e9a074a86755c50afa402c330c5c8cc13c8e0209232e588efd81fd8b6fc43f9",
    "e5fc930399fd488d7fee1c7e50ee04f88a556ce9acb57bcd01df45acffc43cb8",
)
_REVIEWED_QUEUES = frozenset({
    "BOAT_TUTORIAL", "CYNTHIA_JOURNEY", "ENDING_1", "ENDING_2", "ENDING_INTRO", "EPILOGUE",
    "GHOST_IMAGE", "INTRO", "JOURNEY_INTRO", "MANIFEST", "PHONE_TIP", "PIRATE_CAPTAIN",
    "POST_CREDITS", "RUNDUM_REBELLION", "SHAMAN_NAME", "STUDY_ONE", "STUDY_ZERO", "TELEPHONE_0", "TUJU",
})


def _ordered_pairs(left, right):
    """Return matches present in every longest order-preserving alignment."""
    if left == right:
        return list(zip(range(len(left)), range(len(right))))
    rows, cols = len(left), len(right)
    before = [[0] * (cols + 1) for _ in range(rows + 1)]
    after = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i, key in enumerate(left):
        for j, other in enumerate(right):
            before[i + 1][j + 1] = (before[i][j] + 1 if key == other
                                     else max(before[i][j + 1], before[i + 1][j]))
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            after[i][j] = (after[i + 1][j + 1] + 1 if left[i] == right[j]
                           else max(after[i + 1][j], after[i][j + 1]))
    length = before[rows][cols]
    ranks = defaultdict(list)
    for i, key in enumerate(left):
        for j, other in enumerate(right):
            if key == other and before[i][j] + 1 + after[i + 1][j + 1] == length:
                ranks[before[i][j]].append((i, j))
    return [ranks[rank][0] for rank in sorted(ranks) if len(ranks[rank]) == 1]


def _clue_slots(text):
    slots = []
    for index, value in enumerate(text.split("\n")):
        line = value.strip()
        if not line:
            continue
        if line.startswith("@trigger:"):
            slots.append((index, line, ""))  # Control markers anchor the alignment.
            continue
        marker = _DIRECTIVE.match(line)
        slots.append((index, marker[1].lower() if marker else "narration",
                      line[marker.end():] if marker else line))
    return slots


def readable_scene(text, entities, locale):
    def replace(match):
        person, field, other = match.groups()
        name = {"firstname": "firstName", "lastname": "lastName"}.get(field, field or "name")
        key = (person or other).lower()
        return entities[key][locale].get(name, match[0]) if key in entities else match[0]

    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("@trigger:"):
            continue
        marker = _DIRECTIVE.match(line)
        if marker:
            speaker = marker[1].lower()
            line = line[marker.end():]
            if speaker in entities:
                line = entities[speaker][locale].get("name", speaker) + (": " if locale == 0 else "：") + line
            elif speaker == "unknown":
                line = "??? " + line
        lines.append(_REFERENCE.sub(replace, line))
    return "\n".join(lines)


def _renderable_pair(left, right, plain):
    pair = [plain(left), plain(right)]
    if not all(pair):
        return None
    if _CENSORED.search(left) and _CENSORED.search(right):
        pair.extend(plain(_CENSORED.sub("@$*!", text)) for text in (left, right))
    return pair


def _clues(left, right, plain):
    result = {}
    for key in sorted(left.keys() & right.keys()):
        scene = [root[key].get("clue") for root in (left, right) if isinstance(root[key], dict)]
        if len(scene) != 2 or any(not isinstance(value, str) for value in scene):
            continue
        a, b = map(_clue_slots, scene)
        if max(len(a), len(b)) > 240:
            continue
        slots = []
        for i, j in _ordered_pairs([row[1] for row in a], [row[1] for row in b]):
            if texts := _renderable_pair(a[i][2], b[j][2], plain):
                # Keep both original slot numbers. Blank or inserted lines can
                # make the annotation's room/time/index differ by locale.
                slots.append([a[i][0], b[j][0], a[i][1], texts])
        if slots:
            result[key] = slots
    return result


def _dialogue_rows(left, right, reviewed, plain):
    def pairs(a, b, allow_order):
        if not isinstance(a, list) or not isinstance(b, list):
            return
        first, second = [game._anchor(row) for row in a], [game._anchor(row) for row in b]
        if None in first or None in second:
            return
        if allow_order and first == second:
            yield from zip(a, b)
            return
        left_count, right_count = Counter(first), Counter(second)
        by_id = dict(zip(second, b))
        for key, row in zip(first, a):
            if left_count[key] == right_count[key] == 1:
                yield row, by_id[key]

    rows = []
    for scene in sorted(left.keys() & right.keys()):
        a, b = left[scene], right[scene]
        if not isinstance(a, dict) or not isinstance(b, dict):
            continue
        queues = [(a.get("lines"), b.get("lines"), reviewed and scene in _REVIEWED_QUEUES)]
        if reviewed and scene == "STUDY_11":
            queues.extend((en["lines"], zh["lines"], True) for en, zh in zip(a["branches"], b["branches"]))
        for en, zh, aligned in queues:
            for original, translation in pairs(en, zh, aligned):
                if texts := _renderable_pair(original["text"], translation["text"], plain):
                    speakers = [row.get("speaker") or "" for row in (original, translation)]
                    rows.append([speakers, texts])
    return rows


def _entities(left, right):
    result = {}
    for role in ("people", "places", "items"):
        a = {row["id"]: row for row in left.get(role, ()) if isinstance(row, dict) and "id" in row}
        b = {row["id"]: row for row in right.get(role, ()) if isinstance(row, dict) and "id" in row}
        for key in sorted(a.keys() & b.keys()):
            fields = []
            for table in (a, b):
                row = table[key]
                record = {name: row[name] for name in ("name", "firstName", "lastName", "title", "strangername")
                          if isinstance(row.get(name), str) and row[name].strip()}
                if role == "people" and not record.get("name") and record.get("firstName"):
                    record["name"] = " ".join(record[k] for k in ("firstName", "lastName") if k in record)
                fields.append(record)
            result[key] = fields
    return result


def _document_texts(left, right, plain):
    documents = [{row["id"]: row for row in table if isinstance(row, dict) and "id" in row}
                 for table in (left, right)]
    a, b = documents
    pairs = []

    def rendered(doc, body):
        if not isinstance(body, str) or not isinstance(doc.get("title"), str):
            return ""
        text = plain(body)
        return text if doc.get("type") == "rich" else plain(doc["title"]) + "\n\n" + text

    def condition_keys(value):
        if isinstance(value, dict) and value.get("type") == "unlocks" and isinstance(value.get("ids"), list):
            if all(isinstance(key, str) for key in value["ids"]):
                return frozenset(value["ids"])
        return None

    for key in sorted(a.keys() & b.keys()):
        en, zh = a[key], b[key]
        ordinary = [rendered(row, row.get("fullText")) for row in (en, zh)]
        if all(ordinary):
            pairs.append(ordinary)
        variants = [row.get("conditionalContent") for row in (en, zh)]
        if all(isinstance(row, dict) for row in variants):
            conditions = [condition_keys(row.get("condition")) for row in variants]
            if conditions[0] is not None and conditions[0] == conditions[1]:
                alternate = [rendered(doc, row.get("fullText")) for doc, row in zip((en, zh), variants)]
                if all(alternate):
                    pairs.append(alternate)
    return pairs


def _dated_pairs(left, right):
    def key(text, locale):
        if not isinstance(text, str):
            return None
        if locale == "en":
            match = re.fullmatch(r"(?:(Morning|Afternoon|Evening) )?([A-Za-z]+) (\d{1,2})(?:st|nd|rd|th)(?: (\d{4}))?", text)
            if match and match[2] in _MONTHS:
                return int(match[4] or 0), _MONTHS[match[2]], int(match[3]), match[1] or ""
        else:
            match = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:\s*(早|午|晚))?", text)
            if match:
                return int(match[1] or 0), int(match[2]), int(match[3]), _PERIODS.get(match[4], "")
        return None

    if not isinstance(left, list) or not isinstance(right, list):
        return []
    tables = []
    for texts, locale in ((left, "en"), (right, "cn")):
        table = defaultdict(list)
        for text in texts:
            if signature := key(text, locale):
                table[signature].append(text)
        tables.append(table)
    a, b = tables
    return [[a[k][0], b[k][0]] for k in sorted(a.keys() & b.keys()) if len(a[k]) == len(b[k]) == 1]


def _ui_data(archive, plain):
    source = game._ui_source(archive)
    tables, starts = game._ui_tables(source)
    left, right = tables["en"], tables["cn"]
    labels = {key: [plain(left[key]), plain(right[key])] for key in left.keys() & right.keys()
              if isinstance(left[key], str) and isinstance(right[key], str)}
    fixed = {f"#{key.replace('_', '-')}": labels[key] for key in
             ("diff_title", "diff_text", "diff_desc", "diff_normal", "diff_hardcore") if key in labels}
    warning = re.search(r'\{\s*en:\{\s*title:"Content Warning",text:', source)
    if warning:
        values = parse_literal_object(source[warning.start():warning.start() + 9000])
        if values.keys() >= {"en", "cn"}:
            for name in ("title", "text", "censor", "continue"):
                pair = [values[lang].get(name) for lang in ("en", "cn")]
                if all(isinstance(x, str) for x in pair):
                    fixed["#cw-" + name] = list(map(plain, pair))

    def template(locale, key, replacement):
        # The only supported functions here return a template with a single
        # named parameter. Substitute literal UI text without evaluating JS.
        start = starts[locale]
        scope = re.split(r"(?<![\w$])(?:en|cn|fr|es|jp|kr):\s*\{\s*settings_tooltip:",
                         source[start:start + 40_000], maxsplit=1)[0]
        found = re.search(r"\b" + re.escape(key) + r":\s*([A-Za-z_$][\w$]{0,15})\s*=>\s*(`(?:\\.|[^`])*`)", scope)
        if not found:
            return ""
        literal = found[2].replace("${" + found[1] + "}", replacement)
        if "${" in literal:
            return ""
        value = parse_literal_object("{text:" + literal + "}")["text"]
        return plain(value) if isinstance(value, str) else ""

    letter = [template(lang, "intro_letter_html", tables[lang].get("intro_more_files", "")) for lang in ("en", "cn")]
    if all(letter):
        fixed[".letter-body"] = letter
    templates = {"partialEcho": [template(lang, "clue_partial_echo", "__ITEM__") for lang in ("en", "cn")]}
    dates = [pair for key in ("journey_dates", "cynthia_dates") for pair in _dated_pairs(left.get(key), right.get(key))]
    return labels, fixed, templates, dates


def build(archive, profile_pairs, plain):
    pairs = {pair["format"]: pair for pair in profile_pairs}
    main, dialogue = pairs["red_pearls_data"], pairs["red_pearls_dialogue"]
    roots = []
    for locale in ("english", "chinese"):
        tables, _ = game._module(archive, main[locale])
        roots.append({role[0]: table for table in tables if (role := game._role(table))})
    clues = _clues(roots[0]["scenes"], roots[1]["scenes"], plain)
    documents = _document_texts(roots[0].get("documents", []), roots[1].get("documents", []), plain)
    people = _entities(*roots)
    names = [[en[key], zh[key]] for en, zh in people.values() for key in en.keys() & zh.keys()]
    places = [[row["name"] for row in people[place["id"]]] for place in roots[0].get("places", [])
              if place["id"] in people and all("name" in row for row in people[place["id"]])]
    reviewed = tuple(hashlib.sha256(archive.read(dialogue[k])).hexdigest()
                     for k in ("english", "chinese")) == _REVIEWED_DIALOGUE
    queues = [game._module(archive, dialogue[k])[0] for k in ("english", "chinese")]
    spoken = _dialogue_rows(queues[0][0], queues[1][0], reviewed, plain)
    try:
        labels, fixed, templates, dates = _ui_data(archive, plain)
    except (OSError, ValueError, RecursionError):
        labels, fixed, templates, dates = {}, {}, {}, []
    return dict(labels=labels, fixed=fixed, templates=templates, dates=dates,
                entities=people, names=names, places=places, documents=documents, dialogue=spoken, clues=clues)
