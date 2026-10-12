from pathlib import Path
import tempfile
import sqlite3
import unittest

from ayuclicker.storage import SqliteAttempts, load_config, save_config


class StorageTests(unittest.TestCase):
    def test_claim_is_persistent_and_account_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "attempts.sqlite3"
            first = SqliteAttempts(path, account_id=1)
            self.assertTrue(first.claim((5, 10)))
            self.assertFalse(first.claim((5, 10)))
            first.close()
            second = SqliteAttempts(path, account_id=1)
            self.assertTrue(second.contains((5, 10)))
            self.assertFalse(second.contains((6, 10)))
            second.close()
            another_account = SqliteAttempts(path, account_id=2)
            self.assertTrue(another_account.claim((5, 10)))
            another_account.close()

    def test_settings_roundtrip_and_malformed_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(load_config(directory), {})
            value = {"word": "подарок", "button": "Забрать", "minimum": "2,5"}
            save_config(directory, value)
            self.assertEqual(load_config(directory), value)
            self.assertFalse((directory / "settings.json.tmp").exists())
            (directory / "settings.json").write_text("[]", encoding="utf-8")
            self.assertEqual(load_config(directory), {})
            (directory / "settings.json").write_text("invalid", encoding="utf-8")
            self.assertEqual(load_config(directory), {})

    def test_old_attempt_database_is_preserved_and_outcomes_are_account_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "attempts.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE attempts (account INTEGER, chat INTEGER, message INTEGER, PRIMARY KEY(account,chat,message))")
            connection.execute("INSERT INTO attempts VALUES (1, 5, 10)")
            connection.commit()
            connection.close()
            store = SqliteAttempts(path, 1)
            self.assertTrue(store.contains((5, 10)))
            self.assertTrue(store.claim((5, 11)))
            store.complete((5, 11), "mine")
            values = store.connection.execute("SELECT message, outcome FROM attempts ORDER BY message").fetchall()
            self.assertEqual(values, [(10, "legacy_once"), (11, "mine")])
            store.close()
            another = SqliteAttempts(path, 2)
            another.claim((5, 11))
            another.complete((5, 11), "other")
            outcomes = another.connection.execute("SELECT account, outcome FROM attempts WHERE message=11 ORDER BY account").fetchall()
            self.assertEqual(outcomes, [(1, "mine"), (2, "other")])
            another.close()
