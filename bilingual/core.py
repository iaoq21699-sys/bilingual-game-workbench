"""Shared local corpus, dictionary and explicit game-adapter orchestration."""
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import importers

TOKENS = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
PLACEHOLDERS = re.compile(r"\{[^{}]+\}|%(?:\d+\$)?[-+0-9.]*[sdfiu]")
TAGS = re.compile(r"<[^>]*>|\{[^{}]*\}")
# Only color tags observed in supported game text; unknown brackets remain text.
DISPLAY_COLOR_TAGS = re.compile(r"\[/?(?:blue|gold)\]", re.I)
IRREGULAR = {"was": "be", "were": "be", "is": "be", "are": "be", "am": "be", "been": "be",
             "went": "go", "gone": "go", "did": "do", "done": "do", "had": "have", "has": "have",
             "saw": "see", "seen": "see", "took": "take", "taken": "take", "made": "make",
             "thought": "think", "knew": "know", "known": "know", "felt": "feel", "found": "find",
             "got": "get", "gave": "give", "given": "give", "said": "say", "told": "tell",
             "could": "can", "would": "will", "should": "shall", "better": "good", "best": "good",
             "children": "child", "people": "person", "men": "man", "women": "woman", "feet": "foot",
             "teeth": "tooth", "left": "leave", "kept": "keep", "bought": "buy", "brought": "bring"}
CONTRACTIONS = {"don't": "do not", "doesn't": "does not", "didn't": "did not", "can't": "cannot",
                "won't": "will not", "isn't": "is not", "aren't": "are not", "wasn't": "was not",
                "weren't": "were not", "i'm": "I am", "you're": "you are", "we're": "we are",
                "they're": "they are", "it's": "it is / it has", "that's": "that is / that has",
                "i've": "I have", "we've": "we have", "you've": "you have", "i'll": "I will",
                "let's": "let us", "haven't": "have not", "hasn't": "has not", "couldn't": "could not",
                "wouldn't": "would not", "shouldn't": "should not", "i'd": "I would / I had"}


def display_text(value):
    """Hide known presentation tags without changing stored text or parameters."""
    return DISPLAY_COLOR_TAGS.sub("", str(value or ""))


def clean(value):
    return html.unescape(TAGS.sub(" ", display_text(value))).replace("’", "'")


def variants(word):
    """Candidates only; a candidate is never presented as a verified lemma."""
    word = word.lower().replace("’", "'")
    result = [word]
    if word in IRREGULAR:
        result.append(IRREGULAR[word])
    if word.endswith("'s"):
        result.append(word[:-2])
    if word.endswith("ies") and len(word) > 4:
        result.append(word[:-3] + "y")
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stem = word[:-len(suffix)]
            result.extend((stem, stem + "e"))
            if len(stem) > 2 and stem[-1] == stem[-2]:
                result.append(stem[:-1])
    return list(dict.fromkeys(result))


def json_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Service:
    def __init__(self, project=None, data_dir=None):
        self.project = Path(project or Path(__file__).resolve().parents[1])
        self.data_dir = Path(data_dir or self.project / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.data_dir / "corpus.sqlite3"
        self.config_path = self.data_dir / "settings.json"
        self.lock = threading.RLock()
        self.last_report = {}
        self.config = self._read_config()
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS games(
                    id TEXT PRIMARY KEY, title TEXT, root TEXT, adapter TEXT,
                    coverage TEXT, refreshed TEXT);
                CREATE TABLE IF NOT EXISTS entries(
                    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, key TEXT, namespace TEXT,
                    category TEXT, en TEXT, zh TEXT, speaker TEXT, source TEXT, note TEXT,
                    phrases TEXT, warnings TEXT, document TEXT);
                CREATE INDEX IF NOT EXISTS entries_game ON entries(game_id, category);
                CREATE TABLE IF NOT EXISTS lexicon(
                    term TEXT, scope TEXT, meaning TEXT, source TEXT,
                    PRIMARY KEY(term, scope));
                CREATE TABLE IF NOT EXISTS phrases(
                    term TEXT, scope TEXT, meaning TEXT, source TEXT,
                    PRIMARY KEY(term, scope));
            """)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("has_text", 1, lambda value: bool(value and value.strip()))
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_config(self):
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        parent = self.project.parent
        roots = [parent / "lifeisStrange", parent / "Subnautica.2.Build.26496980"]
        return {"roots": [str(p) for p in roots if p.is_dir()], "dictionary_path": ""}

    def _save_config(self):
        candidate = self.config_path.with_suffix(".tmp")
        candidate.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(candidate, self.config_path)

    @staticmethod
    def _adapters():
        from .adapters import lis, subnautica
        return {"lis": lis, "subnautica": subnautica}

    def refresh(self):
        with self.lock:
            results, errors = [], []
            for root in self.config.get("roots", []):
                try:
                    adapter = next((a for a in self._adapters().values() if a.detect(Path(root))), None)
                    if adapter is None:
                        errors.append(f"{root}：未找到支持的游戏资料，已有索引保留。")
                        continue
                    results.append(self._store(adapter.load(Path(root))))
                except Exception as exc:
                    errors.append(f"{root}：{exc}")
            for game_id in self.config.get("profiles", []):
                try:
                    manager = self._profiles()
                    results.append(self._store(manager.payload(manager.load(game_id))))
                except Exception as exc:
                    errors.append(f"{game_id}：{exc}；已有索引保留。")
            dictionary_count = self._extend_dictionary()
            self._save_config()
            self.last_report = dict(games=results, errors=errors, dictionary_added=dictionary_count)
            (self.data_dir / "last-import-report.json").write_text(
                json.dumps(self.last_report, ensure_ascii=False, indent=2), encoding="utf-8")
            return self.last_report

    def add_game_root(self, path):
        root = Path(path).resolve()
        with self.lock:
            adapter = next((a for a in self._adapters().values() if a.detect(root)), None)
            if adapter is None:
                raise ValueError("此目录尚无匹配的游戏适配器。可用「导入文本」或「成对导入」先建立词句库；原生双语显示需要适配该游戏。")
            payload = adapter.load(root)
            result = self._store(payload)
            if payload["root"] not in self.config["roots"]:
                self.config["roots"].append(payload["root"])
            self._save_config()
            self._extend_dictionary()
            return result

    def _store(self, payload):
        rows, identities = [], set()
        game_id = payload["id"]
        for entry in payload["entries"]:
            namespace, key = str(entry.get("namespace", "")), str(entry["key"])
            identity = (namespace, key)
            if identity in identities:
                raise ValueError(f"导入有重复主键，旧索引未更改：{namespace}/{key}")
            identities.add(identity)
            en, zh = str(entry.get("en") or ""), str(entry.get("zh") or "")
            warnings = list(entry.get("warnings", []))
            if not en.strip():
                warnings.append("缺少英文原文。")
            if not zh.strip():
                warnings.append("缺少中文译文。")
            if en.strip() and en.strip() == zh.strip():
                warnings.append("中英内容相同：可能为专名、占位内容或尚未翻译。")
            if en and zh and Counter(PLACEHOLDERS.findall(en)) != Counter(PLACEHOLDERS.findall(zh)):
                warnings.append("中英格式参数不一致；本工具仅收录，未自动改写游戏资源。")
            category = str(entry.get("category") or "未分类")
            speaker = str(entry.get("speaker") or "")
            source = str(entry.get("source") or "")
            note = str(entry.get("note") or "")
            entry_id = hashlib.sha256(json_value([game_id, namespace, key]).encode("utf-8")).hexdigest()[:32]
            document = "\n".join([en, zh, clean(en), clean(zh), key, namespace, speaker, note])
            rows.append((entry_id, game_id, key, namespace, category, en, zh, speaker, source,
                         note, json_value(entry.get("phrases", [])), json_value(list(dict.fromkeys(warnings))), document))
        if not rows:
            raise ValueError("未提取到任何记录；旧索引保留。")
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO games VALUES(?,?,?,?,?,?)", (
                game_id, payload["title"], str(payload["root"]), payload["adapter"],
                payload["coverage"], datetime.now().isoformat(timespec="seconds")))
            db.execute("DELETE FROM entries WHERE game_id=?", (game_id,))
            db.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute("DELETE FROM phrases WHERE scope=?", (game_id,))
            db.execute("DELETE FROM lexicon WHERE scope=?", (game_id,))
            for field, default_scope in (("lexicon", game_id), ("global_lexicon", "global")):
                for term, item in payload.get(field, {}).items():
                    if isinstance(item, str):
                        item = {"meaning": item}
                    db.execute("INSERT OR REPLACE INTO lexicon VALUES(?,?,?,?)", (
                        term.lower(), item.get("scope", default_scope), item.get("meaning", item.get("translation", "")),
                        item.get("source", "原有词典")))
            for item in payload.get("phrases", []):
                term = item.get("term", item.get("en", "")).lower().strip()
                if term:
                    db.execute("INSERT OR REPLACE INTO phrases VALUES(?,?,?,?)", (
                        term, item.get("scope", game_id), item.get("meaning", item.get("zh", "")), item.get("source", "原有注释")))
        return dict(id=game_id, title=payload["title"], count=len(rows),
                    paired=sum(bool(r[5].strip() and r[6].strip()) for r in rows),
                    warnings=sum(r[11] != "[]" for r in rows), coverage=payload["coverage"])

    def _extend_dictionary(self):
        """Stream the existing licensed dictionary; index only corpus vocabulary."""
        candidates = []
        if self.config.get("dictionary_path"):
            candidates.append(Path(self.config["dictionary_path"]).expanduser())
        candidates.append(self.project / "dictionaries/ecdict.csv")
        for root in self.config.get("roots", []):
            p = Path(root)
            candidates.extend((p / "BilingualMod/source/ecdict.csv", p / "source/ecdict.csv"))
        dictionary = next((p for p in candidates if p.is_file()), None)
        if dictionary is None:
            return 0
        with self._db() as db:
            needed = set()
            for row in db.execute("SELECT en FROM entries"):
                for token in TOKENS.findall(clean(row[0])):
                    needed.update(variants(token))
            existing = {r[0] for r in db.execute("SELECT term FROM lexicon WHERE scope='global'")}
            needed.difference_update(existing)
            rows, phrase_rows = [], []
            with dictionary.open(encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                if not {"word", "translation"}.issubset(reader.fieldnames or []):
                    raise ValueError("离线词典须为 ECDICT 格式 CSV，表头至少包含 word 和 translation。")
                for item in reader:
                    word = (item.get("word") or "").strip().lower()
                    if not word:
                        continue
                    if word in needed and item.get("translation"):
                        meaning = item["translation"].replace("\\n", "\n")
                        if item.get("phonetic"):
                            meaning = f"/{item['phonetic']}/  {meaning}"
                        rows.append((word, "global", meaning, "ECDICT 通用词典（未作逐句义项判定）"))
                    if (" " in word and 2 <= len(word.split()) <= 8 and item.get("translation")
                            and re.fullmatch(r"[a-z]+(?:[ '\-][a-z]+)+", word)):
                        phrase_rows.append((word, "global", item["translation"].replace("\\n", "\n"),
                                            "ECDICT 通用词组（需结合句意，非逐句审校）"))
            db.executemany("INSERT OR IGNORE INTO lexicon VALUES(?,?,?,?)", rows)
            db.executemany("INSERT OR IGNORE INTO phrases VALUES(?,?,?,?)", phrase_rows)
        return len(rows)

    def import_dictionary(self, path):
        """Select a user's local ECDICT-format CSV without copying the large file."""
        dictionary = Path(path).expanduser().resolve()
        if not dictionary.is_file():
            raise ValueError("所选离线词典文件不存在。")
        try:
            with dictionary.open(encoding="utf-8-sig", newline="") as file:
                fields = csv.DictReader(file).fieldnames or []
                if not {"word", "translation"}.issubset(fields):
                    raise ValueError("离线词典须为 ECDICT 格式 CSV，表头至少包含 word 和 translation。")
        except UnicodeError as exc:
            raise ValueError("离线词典需要使用 UTF-8 编码的 CSV 文件。") from exc
        with self.lock:
            previous = self.config.get("dictionary_path", "")
            self.config["dictionary_path"] = str(dictionary)
            try:
                added = self._extend_dictionary()
                self._save_config()
            except BaseException:
                self.config["dictionary_path"] = previous
                raise
        return (f"已选择离线词典：\n{dictionary}\n\n"
                f"本次为已收录游戏新增 {added:,} 个单词释义，并载入可用通用词组。\n"
                "未复制或上传词典文件。之后接入新游戏时会继续按需补充词义；请保留该文件。")

    def games(self):
        with self._db() as db:
            games = [dict(r) for r in db.execute("""SELECT g.*, count(e.id) count,
                sum(CASE WHEN has_text(e.en) AND has_text(e.zh) THEN 1 ELSE 0 END) paired
                FROM games g LEFT JOIN entries e ON g.id=e.game_id GROUP BY g.id ORDER BY g.title""")]
        for game in games:
            game["supports_native"] = game["adapter"] in ("lis", "subnautica", "configured")
            game["supports_patch"] = game["supports_native"]
            game["supports_launcher"] = game["adapter"] == "configured"
            try:
                if game["adapter"] == "configured":
                    manager = self._profiles()
                    profile = manager.load(game["id"])
                    game["engine"] = profile["engine"]
                    game["corpus_only"] = manager.corpus_only(profile)
                    if game["corpus_only"]:
                        game["supports_patch"] = False
                        game["supports_launcher"] = False
                    if profile["engine"] == "electron":
                        game["panel_backend"] = "electron"
                        game["coverage"] = game["coverage"].removesuffix("游戏仍为原版显示。")
                game["status"] = self.action(game["id"], "status") if game["adapter"] != "import" else "文本库 / 未接入原生显示"
            except Exception as exc:
                game["status"] = f"待检查：{exc}"
        return games

    def game_info(self, game_id):
        return next((g for g in self.games() if g["id"] == game_id), {})

    def categories(self, game_id=None):
        with self._db() as db:
            query = "SELECT DISTINCT category FROM entries"
            params = ()
            if game_id:
                query += " WHERE game_id=?"
                params = (game_id,)
            return [r[0] for r in db.execute(query + " ORDER BY category", params)]

    def search(self, query="", game_id=None, category="", limit=150, offset=0):
        clauses, params = [], []
        if game_id:
            clauses.append("game_id=?")
            params.append(game_id)
        if category:
            clauses.append("category=?")
            params.append(category)
        for token in query.strip().split()[:12]:
            clauses.append("document LIKE ? ESCAPE '\\'")
            params.append("%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            total = db.execute("SELECT count(*) FROM entries" + where, params).fetchone()[0]
            rows = db.execute("SELECT id,game_id,key,namespace,category,en,zh,speaker,source FROM entries" + where
                              + " ORDER BY game_id,category,key,id LIMIT ? OFFSET ?", params + [min(max(1, limit), 500), max(0, offset)])
            return dict(total=total, items=[dict(row) for row in rows])

    def _lookup(self, db, term, game_id=None):
        normalized = term.lower().strip().replace("’", "'")
        scopes = (game_id or "", "global")
        # A known inflection gets its own dictionary entry; fallback is labelled explicitly.
        for candidate in variants(normalized):
            matches = [dict(r) for r in db.execute(
                "SELECT term,meaning,source,scope FROM lexicon WHERE term=? AND scope IN (?,?) ORDER BY CASE WHEN scope=? THEN 0 ELSE 1 END",
                (candidate, *scopes, game_id or ""))]
            if matches:
                for match in matches:
                    if candidate != normalized:
                        match["source"] += f"；词形候选 {normalized} → {candidate}，需结合句意"
                return matches
        if normalized in CONTRACTIONS:
            return [dict(term=term, meaning=f"缩写展开：{CONTRACTIONS[normalized]}", source="常见缩写；多义展开需结合上下文")]
        matches = [dict(r) for r in db.execute("SELECT term,meaning,source FROM phrases WHERE term=? AND scope IN (?,?)", (normalized, *scopes))]
        return matches or [dict(term=term, meaning="本地词典未收录，可能是专名、游戏造词或词形变化。", source="未收录")]

    def lookup(self, term, game_id=None):
        with self._db() as db:
            return self._lookup(db, term, game_id)

    def entry(self, entry_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise ValueError("此记录已更新，请重新搜索。")
            result = dict(row)
            result.pop("document", None)
            result["warnings"] = json.loads(result["warnings"])
            result["phrases"] = json.loads(result["phrases"])
            result["context_phrases"] = list(result["phrases"])
            sentence = clean(result["en"]).lower()
            seen = {p.get("term", "").lower() for p in result["phrases"]}
            # Query n-grams rather than run thousands of phrase regexes for every selection.
            tokens = TOKENS.findall(sentence)
            grams = {" ".join(tokens[i:i+n]) for n in range(2, 9) for i in range(max(0, len(tokens)-n+1))}
            for start in range(0, len(grams), 400):
                group = sorted(grams)[start:start+400]
                matches = db.execute("SELECT term,meaning,source FROM phrases WHERE scope IN (?,?) AND term IN ("
                                     + ",".join("?" for _ in group) + ") ORDER BY length(term) DESC", [result["game_id"], "global", *group])
                for match in matches:
                    term = match["term"]
                    if term not in seen and re.search(r"(?<![a-z])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z])", sentence):
                        seen.add(term)
                        result["phrases"].append(dict(match))
            words = []
            unique = list(dict.fromkeys(TOKENS.findall(clean(result["en"]))))
            for word in unique[:120]:
                hits = self._lookup(db, word, result["game_id"])
                words.append(dict(term=word, meaning=hits[0]["meaning"], source=hits[0]["source"]))
            result["words"] = words
            if len(unique) > 120:
                result["warnings"].append("长文先列出前 120 个不同单词；其余单词可点击查询。")
            return result

    def import_file(self, path, title):
        with self.lock:
            result = self._store(importers.import_file(path, title))
            self._extend_dictionary()
            return result

    def import_pair(self, en_path, zh_path, title):
        with self.lock:
            result = self._store(importers.import_pair(en_path, zh_path, title))
            self._extend_dictionary()
            return result

    def export_game(self, game_id, path):
        destination = Path(path).resolve()
        # Only export text packages; never permit replacing the database/settings or game resources.
        if destination.suffix.lower() != ".json":
            raise ValueError("请导出为 .json 文本包。")
        if destination == self.config_path.resolve() or self.data_dir.resolve() in destination.parents:
            raise ValueError("请选择 data 目录以外的导出位置。")
        with self._db() as db:
            game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not game:
                raise ValueError("请先选择游戏。")
            for known in db.execute("SELECT root FROM games WHERE adapter<>'import'"):
                root = Path(known["root"]).resolve()
                if destination == root or root in destination.parents:
                    raise ValueError("请导出到所有已接入游戏的目录以外，避免覆盖游戏资料。")
            entries = [dict(row) for row in db.execute("SELECT key,namespace,category,en,zh,speaker,source,note,phrases,warnings FROM entries WHERE game_id=? ORDER BY namespace,key", (game_id,))]
            for entry in entries:
                entry["phrases"] = json.loads(entry["phrases"])
                entry["warnings"] = json.loads(entry["warnings"])
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(dict(schema_version=1, title=game["title"], entries=entries), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
        return len(entries)

    def action(self, game_id, action):
        if action not in ("status", "enable", "disable", "launch"):
            raise ValueError("未知操作。")
        with self._db() as db:
            game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if game is None:
            raise ValueError("未选择游戏。")
        if game["adapter"] == "import":
            if action == "status":
                return "文本库 / 未接入原生显示"
            raise ValueError("此游戏只导入了文本，尚无原生显示适配器。")
        with self.lock:
            if game["adapter"] == "configured":
                return self._profiles().action(game_id, action)
            result = self._adapters()[game["adapter"]].action(Path(game["root"]), action)
            if action == "launch":
                from .native_overlay import installed
                if installed(self, game_id):
                    return "已启动游戏，F8 打开游戏内半透明检索；外观按这款游戏单独保存。"
            return result

    def _profiles(self):
        from .profiles import Profiles
        return Profiles(self)

    def inspect_game(self, path):
        return self._profiles().inspect(path)

    def read_profile(self, path, root=None):
        return self._profiles().read(path, root)

    def connect_game(self, probe):
        with self.lock:
            return self._profiles().connect(probe)

    def export_profile(self, game_id, path):
        return self._profiles().export(game_id, path)

    def create_launcher(self, game_id):
        return self._profiles().create_launcher(game_id)

    def session_info(self, game_id):
        with self._db() as db:
            game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not game:
            raise ValueError("找不到所选游戏。")
        if game["adapter"] == "configured":
            return self._profiles().session_info(game_id)
        root = Path(game["root"])
        if game["adapter"] == "lis":
            folder = root / "Life.is.Strange.Remastered.Build.11330282"
            executable = folder / "LiS.exe"
            window_executable = next((folder / "LIS/Binaries/Win64").glob("*Shipping.exe"), executable)
        elif game["adapter"] == "subnautica":
            executable = root / "Subnautica2.exe"
            window_executable = root / "Subnautica2/Binaries/Win64/Subnautica2-Win64-Test.exe"
        else:
            raise ValueError("此文本库没有游戏启动配置。")
        return dict(executable=str(executable), window_executable=str(window_executable), launch_args=[])
