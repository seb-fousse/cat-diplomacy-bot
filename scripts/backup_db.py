"""Copy the game database to a single standalone file, safely even while the bot is running.

The database runs in WAL mode, so copying cat_game.db on its own can miss recent writes;
SQLite's backup API reads a consistent snapshot that includes them.

    python scripts/backup_db.py [destination]

Reads the database from DB_PATH (as the bot does) and writes to data/backup.db by default."""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    source = Path(os.getenv("DB_PATH", ROOT / "cat_game.db"))
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "backup.db"
    if not source.exists():
        print(f"No database at {source}")
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"Backed up {source} -> {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
