import argparse
import sqlite3
from pathlib import Path

from database import DB_PATH, SCHEMA_VERSION, init_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or migrate the MC-QQ-Auth SQLite database.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"SQLite database path (default: {DB_PATH})")
    args = parser.parse_args()

    try:
        init_database(args.db)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"SQLite migration failed: {exc}\n")

    print(f"SQLite schema {SCHEMA_VERSION} is ready.")


if __name__ == "__main__":
    main()
