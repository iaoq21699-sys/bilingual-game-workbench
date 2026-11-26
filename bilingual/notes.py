"""Store game-captured snapshots and edit their personal remarks."""
from contextlib import closing
import sqlite3
import time


def note_key(value):
    from .core import TOKENS

    return " ".join(TOKENS.findall(value.replace("’", "'").lower()))


class Notebook:
    def __init__(self, data_dir):
        self.path = data_dir / "notes.sqlite3"

    def _connect(self, mode):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        return db

    def listing(self, game_id=None):
        if not self.path.exists():
            return [], []
        with closing(self._connect("ro")) as db:
            db.execute("BEGIN")
            # The first capture may be creating the table in another process.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone():
                return [], []
            games = db.execute("""SELECT game_id,game_title FROM notes
                WHERE id IN (SELECT max(id) FROM notes GROUP BY game_id) ORDER BY game_title""").fetchall()
            where = " WHERE game_id=?" if game_id else ""
            rows = db.execute("""SELECT id,kind,target,game_title,substr(en,1,160) AS en,
                                     substr(zh,1,160) AS zh,saved_at_ms
                              FROM notes""" + where + " ORDER BY saved_at_ms DESC,id DESC",
                              (game_id,) if game_id else ()).fetchall()
        return games, rows

    def get(self, note_id):
        with closing(self._connect("ro")) as db:
            return db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()

    def save_remark(self, note_id, text):
        with closing(self._connect("rw")) as db, db:
            changed = db.execute("UPDATE notes SET personal_note=? WHERE id=?", (text, note_id))
            if not changed.rowcount:
                raise ValueError("这条笔记已不存在，备注仍保留在输入框中。")

    def capture(self, entry, title, kind, target, target_key, meaning="", meaning_source=""):
        """Store a web-panel snapshot in the same notebook as the native add-on."""
        with closing(self._connect("rwc")) as db, db:
            db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, game_title TEXT NOT NULL,
                    entry_id TEXT NOT NULL, source_key TEXT NOT NULL, namespace TEXT NOT NULL,
                    entry_source TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('sentence','word','phrase')),
                    target TEXT NOT NULL, target_key TEXT NOT NULL, en TEXT NOT NULL, zh TEXT NOT NULL,
                    meaning TEXT NOT NULL, meaning_source TEXT NOT NULL, saved_at_ms INTEGER NOT NULL,
                    personal_note TEXT NOT NULL DEFAULT '', UNIQUE(game_id, entry_id, target_key)
                );
                CREATE INDEX IF NOT EXISTS notes_by_game_time ON notes(game_id, saved_at_ms DESC, id DESC);
            """)
            changed = db.execute("""INSERT INTO notes(game_id,game_title,entry_id,source_key,namespace,
                entry_source,kind,target,target_key,en,zh,meaning,meaning_source,saved_at_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id,entry_id,target_key) DO NOTHING""",
                (entry["game_id"], title, entry["id"], entry["key"], entry["namespace"], entry["source"],
                 kind, target, target_key, entry["en"], entry["zh"], meaning, meaning_source, time.time_ns() // 1_000_000))
            return changed.rowcount == 1
