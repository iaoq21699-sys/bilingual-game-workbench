"""Exercise backwards compatibility without touching the user's notebook."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bilingual.notes import Notebook


class NotesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.book = Notebook(Path(directory.name))
        self.entry = dict(game_id="fiction", id="short", key="short", namespace="dialogue",
                          source="fixture", en="Run!", zh="快跑！")

    def save(self, kind, key):
        return self.book.capture(self.entry, "示例游戏", kind, "Run!" if kind == "sentence" else "Run",
                                 key, "跑开" if kind == "word" else "")

    def legacy(self, already_in_native_panel):
        self.assertTrue(self.save("sentence", ""))
        original, = self.book.listing()[1]
        self.book.save_remark(original["id"], "原平台备注")
        with closing(sqlite3.connect(self.book.path)) as db, db:
            db.execute("UPDATE notes SET target_key='run' WHERE id=?", (original["id"],))
            if already_in_native_panel:
                db.execute("""INSERT INTO notes(game_id,game_title,entry_id,source_key,namespace,
                    entry_source,kind,target,target_key,en,zh,meaning,meaning_source,saved_at_ms,personal_note)
                    SELECT game_id,game_title,entry_id,source_key,namespace,entry_source,kind,
                           '', '', en,zh,meaning,meaning_source,saved_at_ms,'另一个备注'
                    FROM notes WHERE id=?""", (original["id"],))
        return original["id"]

    def check_legacy_collision(self, already_in_native_panel):
        original_id = self.legacy(already_in_native_panel)
        self.assertFalse(self.save("sentence", ""))
        self.assertEqual(self.book.get(original_id)["target_key"], "run")  # no eager migration
        self.assertTrue(self.save("word", "run"))
        self.assertFalse(self.save("word", "run"))
        self.assertFalse(self.save("sentence", ""))
        original = self.book.get(original_id)
        self.assertEqual((original["kind"], original["personal_note"], original["zh"]),
                         ("sentence", "原平台备注", "快跑！"))
        self.assertEqual(original["target_key"], f":legacy-sentence:{original_id}" if already_in_native_panel else "")
        rows = [self.book.get(row["id"]) for row in self.book.listing()[1]]
        self.assertEqual(len(rows), 3 if already_in_native_panel else 2)
        word, = [row for row in rows if row["kind"] == "word"]
        self.assertEqual((word["target_key"], word["meaning"]), ("run", "跑开"))
        if already_in_native_panel:
            self.assertIn("另一个备注", [row["personal_note"] for row in rows])

    def test_legacy_sentence_releases_a_word_key_without_losing_its_remark(self):
        self.check_legacy_collision(False)

    def test_two_old_sentence_copies_with_different_remarks_are_both_preserved(self):
        self.check_legacy_collision(True)

    def test_failed_word_insert_rolls_back_legacy_key_change(self):
        original_id = self.legacy(False)
        with closing(sqlite3.connect(self.book.path)) as db, db:
            db.execute("""CREATE TRIGGER fail_word BEFORE INSERT ON notes
                          WHEN NEW.kind='word' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture rollback"):
            self.save("word", "run")
        self.assertEqual(self.book.get(original_id)["target_key"], "run")
        self.assertEqual(self.book.get(original_id)["personal_note"], "原平台备注")

    def test_explicit_resave_enriches_only_missing_vocabulary(self):
        for i, (kind, target, missing, source) in enumerate([
            ("word", "Run", "", ""),
            ("phrase", "Run fast", "本地词典暂无对应释义", "未收录"),
        ]):
            with self.subTest(kind=kind):
                entry = dict(self.entry, id=f"missing-{i}")
                def save(meaning, origin):
                    return self.book.capture(entry, "示例游戏", kind, target, target.lower(), meaning, origin)

                self.assertTrue(save(missing, source))
                note_id = next(row["id"] for row in self.book.listing()[1] if row["kind"] == kind)
                self.book.save_remark(note_id, "自己写的备注")
                original = dict(self.book.get(note_id))
                self.assertFalse(save("还是没有释义", "未收录"))
                self.assertTrue(save("新释义", "离线词典"))
                self.assertEqual(dict(self.book.get(note_id)),
                                 dict(original, meaning="新释义", meaning_source="离线词典"))
                self.assertFalse(save("不要覆盖", "第二部词典"))
                self.assertEqual(self.book.get(note_id)["meaning"], "新释义")
                self.assertEqual(len(self.book.listing()[1]), i + 1)
