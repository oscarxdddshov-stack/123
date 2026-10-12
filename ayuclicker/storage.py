"""Local settings and durable, account-scoped click deduplication."""

import json
import os
import sqlite3
from pathlib import Path


def data_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    directory = base / "AyuClicker"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def load_config(directory: Path) -> dict:
    try:
        value = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(directory: Path, config: dict) -> None:
    temporary = directory / "settings.json.tmp"
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(directory / "settings.json")


class SqliteAttempts:
    def __init__(self, path: Path, account_id: int):
        self.connection = sqlite3.connect(path)
        self.account_id = account_id
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS attempts ("
            "account INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, "
            "PRIMARY KEY (account, chat, message))"
        )
        self.connection.commit()
        path.chmod(0o600)

    def contains(self, key: tuple[int, int]) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM attempts WHERE account=? AND chat=? AND message=?",
            (self.account_id, *key),
        ).fetchone() is not None

    def claim(self, key: tuple[int, int]) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO attempts (account, chat, message) VALUES (?, ?, ?)",
                (self.account_id, *key),
            )
        return cursor.rowcount == 1

    def close(self) -> None:
        self.connection.close()
