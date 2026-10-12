from pathlib import Path
import tempfile
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
