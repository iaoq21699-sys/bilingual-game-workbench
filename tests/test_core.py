"""Integrity checks for local corpus imports; never touch installed games."""
import json
from pathlib import Path
import tempfile
import unittest

from bilingual import importers
from bilingual.core import Service


class CorpusIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bilingual-integrity-")
        self.root = Path(self.temp.name)
        self.service = Service(project=self.root, data_dir=self.root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def payload(self, game="sample", entries=None, **extra):
        return dict(id=game, title=game, root=str(self.root / game), adapter="import",
                    coverage="test fixture", entries=entries or [dict(key="one", en="Hello", zh="你好")], **extra)

    def test_pairing_uses_namespace_key_and_keeps_unpaired_rows(self):
        en = self.write_json("en.json", {"entries": [
            dict(namespace="menu", key="one", text="Play"),
            dict(namespace="dialogue", key="one", text="Come here"),
            dict(namespace="menu", key="only-en", text="Missing translation"),
        ]})
        zh = self.write_json("zh.json", {"entries": [
            dict(namespace="dialogue", key="one", text="过来"),
            dict(namespace="menu", key="only-zh", text="仅中文"),
            dict(namespace="menu", key="one", text="开始"),
        ]})
        payload = importers.import_pair(en, zh, "Pair fixture")
        indexed = {(r["namespace"], r["key"]): (r["en"], r["zh"]) for r in payload["entries"]}
        self.assertEqual(indexed, {
            ("menu", "one"): ("Play", "开始"),
            ("dialogue", "one"): ("Come here", "过来"),
            ("menu", "only-en"): ("Missing translation", ""),
            ("menu", "only-zh"): ("", "仅中文"),
        })
        result = self.service._store(payload)
        self.assertEqual((result["count"], result["paired"]), (4, 2))

    def test_duplicate_reimport_and_transaction_failure_preserve_previous_data(self):
        valid = self.payload()
        self.service._store(valid)
        duplicate = self.payload(entries=[dict(key="same", en="A"), dict(key="same", en="B")])
        with self.assertRaises(ValueError):
            self.service._store(duplicate)
        self.assertEqual(self.service.search(game_id="sample")["items"][0]["en"], "Hello")
        # Failure after DELETE/INSERT must still roll the whole transaction back.
        broken = self.payload(entries=[dict(key="changed", en="Changed")], lexicon={"word": []})
        with self.assertRaises((AttributeError, TypeError, ValueError)):
            self.service._store(broken)
        rows = self.service.search(game_id="sample")["items"]
        self.assertEqual([(r["key"], r["en"]) for r in rows], [("one", "Hello")])

    def test_search_special_characters_are_literal_and_parameters_are_flagged(self):
        self.service._store(self.payload(entries=[
            dict(key="discount", en="Save 50% now", zh="节省 50%"),
            dict(key="underscore", en="save_file", zh="存档"),
            dict(key="backslash", en=r"folder\file", zh="文件"),
            dict(key="normal", en="Save 500 now", zh="普通"),
            dict(key="format", en="Hello {name}: %d", zh="你好 {name}"),
        ]))
        for term, expected in (("50%", "discount"), ("_", "underscore"), ("\\", "backslash")):
            with self.subTest(term=term):
                results = self.service.search(term)
                self.assertEqual([r["key"] for r in results["items"]], [expected])
        self.assertEqual(self.service.search("' OR 1=1 --")["total"], 0)
        record = self.service.search(game_id="sample")["items"]
        record = next(row for row in record if row["key"] == "format")
        self.assertTrue(any("格式参数不一致" in warning for warning in self.service.entry(record["id"])["warnings"]))

    def test_game_specific_words_and_phrases_do_not_leak_between_games(self):
        for game, word, phrase in (("a", "甲游戏义项", "甲语境"), ("b", "乙游戏义项", "乙语境")):
            self.service._store(self.payload(
                game, entries=[dict(key="one", en="Find the power cell", zh="找到能源电池")],
                lexicon={"cell": dict(meaning=word)},
                phrases=[dict(term="power cell", meaning=phrase, source=game)],
            ))
        self.assertEqual(self.service.lookup("cell", "a")[0]["meaning"], "甲游戏义项")
        self.assertEqual(self.service.lookup("cell", "b")[0]["meaning"], "乙游戏义项")
        for game, expected in (("a", "甲语境"), ("b", "乙语境")):
            entry_id = self.service.search(game_id=game)["items"][0]["id"]
            self.assertEqual([p["meaning"] for p in self.service.entry(entry_id)["phrases"]], [expected])

    def test_invalid_imports_are_rejected_before_replacing_existing_index(self):
        path = self.write_json("package.json", [dict(key="one", en="Original", zh="原文")])
        result = self.service.import_file(path, "Original")
        invalid = [
            '[{"key":"x","en":"A"},{"key":"x","en":"B"}]',
            '[{"key":null,"en":"A"}]',
            '[{"key":"x","en":"A","phrases":[{"term":123,"meaning":"bad"}]}]',
        ]
        for content in invalid:
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.service.import_file(path, "Invalid")
                self.assertEqual(self.service.search(game_id=result["id"])["items"][0]["en"], "Original")
        duplicate_map = self.root / "duplicate.json"
        duplicate_map.write_text('{"same":"first","same":"second"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            importers.language_map(duplicate_map)

    def test_export_does_not_overwrite_any_registered_game_resources(self):
        self.service._store(self.payload("a"))
        for game in ("native-a", "native-b"):
            payload = self.payload(game)
            payload["adapter"] = "fixture-native"
            self.service._store(payload)
            native = Path(payload["root"])
            native.mkdir()
            resource = native / "reference_data.json"
            resource.write_text("keep original", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.service.export_game("a", resource)
            self.assertEqual(resource.read_text(encoding="utf-8"), "keep original")
        destination = self.root / "export.json"
        self.assertEqual(self.service.export_game("a", destination), 1)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["entries"][0]["en"], "Hello")


if __name__ == "__main__":
    unittest.main()
