"""Exercise the actual desktop routes on Tk; fixture text is original."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from bilingual.core import Service
from bilingual.notes import Notebook
from bilingual.ui import Desktop


class DesktopLearningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.service = Service(self.base / "workbench")
        path = self.base / "sentences.json"
        path.write_text(json.dumps([
            dict(key="one", en="Keep a seat for me.", zh="帮我留个位子。",
                 phrases=[dict(term="keep a seat", meaning="留个座位")]),
            dict(key="two", en="Take the quiet path.", zh="走那条安静的小路。")
        ], ensure_ascii=False), encoding="utf8")
        self.gid = self.service.import_file(path, "虚构游戏")["id"]
        try:
            self.app = Desktop(self.service)
        except tk.TclError as exc:
            if "display" in str(exc).lower():
                self.skipTest("A desktop display is needed for the real Tk route")
            raise
        self.addCleanup(self._close)
        self.errors = []
        self.app.root.report_callback_exception = lambda *error: self.errors.append(error)
        self._wait(lambda: self.gid in self.app.game_ids)

    def _close(self):
        if not self.app.closed:
            self.app.operation_busy = False
            root = self.app.root
            def widgets(widget):
                yield widget
                for child in widget.winfo_children():
                    yield from widgets(child)
            owners = {name: widget for widget in widgets(root) for name in (widget._tclCommands or [])}
            for callback in root.tk.splitlist(root.tk.call("after", "info")):
                script, _ = root.tk.splitlist(root.tk.call("after", "info", callback))
                if script in owners:
                    owners[script].after_cancel(callback)
            self.app.close()

    def _wait(self, predicate):
        stop = time.monotonic() + 5
        while not predicate() and time.monotonic() < stop and not self.errors:
            self.app.root.update()
            time.sleep(.01)
        self.assertFalse(self.errors, self.errors)
        self.assertTrue(predicate())

    def test_desktop_search_capture_and_active_recall_share_one_notebook(self):
        a = self.app
        a.game_picker.current(a.game_ids.index(self.gid))
        a._game_selected()
        a.query.set("seat")
        self._wait(lambda: a.current_entry and a.current_entry["key"] == "one" and a.total == 1)
        a.save_sentence_button.invoke()
        self._wait(lambda: not a.note_busy)
        a.save_sentence_button.invoke()
        self._wait(lambda: not a.note_busy)
        self.assertIn("已在收藏", a.status.get())
        a.lookup_query.set("keep a seat")
        a._lookup()
        self._wait(lambda: a.lookup_note is not None)
        a.save_term_button.invoke()
        self._wait(lambda: not a.note_busy)
        book = Notebook(self.service.data_dir)
        records = [dict(book.get(row["id"])) for row in book.listing(self.gid)[1]]
        self.assertEqual({row["kind"] for row in records}, {"phrase", "sentence"})
        self.assertEqual(records[0]["meaning"], "留个座位")
        a._open_notes()
        n = a.notes_window
        self.app.root.update()
        self.assertIn("帮我留个位子", n.detail.get("1.0", "end"))
        n.review_toggle.invoke()
        self.assertNotIn("帮我留个位子", n.detail.get("1.0", "end"))
        self.assertFalse(n.list.show_secondary)
        self.assertNotIn("留个座位", n.detail.get("1.0", "end"))
        self.assertFalse(n.remark.winfo_manager())
        n.reveal_button.invoke()
        self.assertIn("帮我留个位子", n.detail.get("1.0", "end"))
        self.assertIn("留个座位", n.detail.get("1.0", "end"))
        n.remark.insert("end", "我的备注")
        first = n.current["id"]
        n.next_button.invoke()
        self.assertNotEqual(n.current["id"], first)
        self.assertNotIn("帮我留个位子", n.detail.get("1.0", "end"))
        self.assertEqual(book.get(first)["personal_note"], "我的备注")
        n.review_toggle.invoke()
        self.assertIn("帮我留个位子", n.detail.get("1.0", "end"))
        self.assertTrue(n.list.show_secondary)
        a.hide()
        self.assertEqual(n.window.state(), "withdrawn")
        self.assertTrue(n.window.winfo_exists())

    def test_real_desktop_fallback_path_restores_only_the_captured_session(self):
        a = self.app
        session = Mock()
        session.capture_foreground.return_value = True
        session.restore_focus.return_value = True
        a.sessions = {self.gid: session}
        with patch("bilingual.native_overlay.installed", return_value=True):
            a.toggle()
        self.assertIs(a.return_session, session)
        self.assertEqual(a.game_id, self.gid)
        session.restore_focus.assert_not_called()
        a.hide()
        session.restore_focus.assert_called_once_with()

    def test_blocked_wizard_exposes_routes_not_an_install_button(self):
        a = self.app
        choices = Mock()
        a._import_menu = choices
        report = self.service.inspect_game(self.base)
        a._show_connect_wizard(report)
        windows = [widget for widget in a.root.winfo_children() if isinstance(widget, tk.Toplevel)]
        diagnostic = windows[-1]
        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)
        buttons = {widget.cget("text"): widget for widget in walk(diagnostic)
                   if widget.winfo_class() == "TButton"}
        self.assertEqual(set(buttons), {"重选目录", "导入接入配置", "仅导入文本", "关闭"})
        buttons["仅导入文本"].invoke()
        choices.assert_called_once_with()
        self.assertFalse(diagnostic.winfo_exists())
