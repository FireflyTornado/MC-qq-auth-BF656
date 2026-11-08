import re
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "auth.db"
SCHEMA_VERSION = 1
MAX_AUTH_VERSION = (1 << 63) - 1

MIGRATION_PATH = Path(__file__).with_name("migrations") / "001_offline_auth.sql"
ACTIVE_INDEXES = {
    "idx_offline_active_qq": ("group_id", "qq_id"),
    "idx_offline_active_name": ("mc_name_key",),
}
CURRENT_TABLES = {
    "auth_state": {"id": "INTEGER", "version": "INTEGER"},
    "offline_bindings": {
        "id": "INTEGER",
        "group_id": "TEXT",
        "qq_id": "TEXT",
        "mc_name": "TEXT",
        "mc_name_key": "TEXT",
        "status": "TEXT",
        "bound_at": "INTEGER",
        "updated_at": "INTEGER",
    },
}
LEGACY_TABLES = {"bindings", "pending_bindings"}


class MigrationError(sqlite3.DatabaseError):
    """The database cannot safely be initialized or upgraded."""


def _apply_initial_schema(conn: sqlite3.Connection) -> None:
    # executescript() can commit the open transaction before applying DDL.
    statement = ""
    for line in MIGRATION_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise MigrationError("Incomplete statement in the initial migration")


def _validate_table(conn: sqlite3.Connection, table: str) -> None:
    columns = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    types = {name.lower(): kind.upper() for _, name, kind, *_ in columns}
    if types != CURRENT_TABLES[table] or any(
        hidden or pk != (name.lower() == "id") or (not required and not pk)
        for _, name, _, required, _, pk, hidden in columns
    ):
        raise MigrationError(f"Incompatible columns or primary key in {table}")

    # An INTEGER PRIMARY KEY must alias the rowid, since inserts omit the ID.
    indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
    if any(origin == "pk" for _, _, _, origin, _ in indexes):
        raise MigrationError(f"{table}.id must be an INTEGER PRIMARY KEY that aliases rowid")

    trigger = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'trigger' AND lower(tbl_name) = ?",
        (table,),
    ).fetchone()
    if trigger:
        raise MigrationError(f"Unexpected trigger on {table}")


def _validate_auth_state(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, version FROM auth_state LIMIT 2").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise MigrationError("auth_state must contain exactly one row with id = 1")
    version = rows[0][1]
    if not isinstance(version, int) or not 0 <= version < MAX_AUTH_VERSION:
        raise MigrationError("auth_state.version must be a nonnegative integer with room to increment")


def _validate_offline_bindings(conn: sqlite3.Connection) -> None:
    invalid = conn.execute(
        """
        SELECT 1
        FROM offline_bindings
        WHERE typeof(group_id) != 'text'
           OR typeof(qq_id) != 'text'
           OR typeof(mc_name) != 'text'
           OR length(mc_name) NOT BETWEEN 3 AND 16
           OR mc_name GLOB '*[^A-Za-z0-9_]*'
           OR instr(mc_name, char(0)) != 0
           OR typeof(mc_name_key) != 'text'
           OR mc_name_key COLLATE BINARY != lower(mc_name)
           OR typeof(status) != 'text'
           OR status COLLATE BINARY NOT IN ('ACTIVE', 'REVOKED')
           OR typeof(bound_at) != 'integer'
           OR typeof(updated_at) != 'integer'
        LIMIT 1
        """
    ).fetchone()
    if invalid:
        raise MigrationError("offline_bindings contains an invalid name, lowercase key, status or field type")

    unique_indexes = {
        name.lower(): (name, partial)
        for _, name, unique, _, partial in conn.execute("PRAGMA index_list(offline_bindings)")
        if unique
    }
    if unique_indexes.keys() != ACTIVE_INDEXES.keys():
        raise MigrationError("offline_bindings is missing an expected unique index or has an unexpected one")

    for expected_name, columns in ACTIVE_INDEXES.items():
        name, partial = unique_indexes[expected_name]
        index_columns = [
            ((column or "").lower(), (collation or "").upper())
            for column, collation in conn.execute(
                "SELECT name, coll FROM pragma_index_xinfo(?) WHERE key ORDER BY seqno",
                (name,),
            )
        ]
        sql = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        predicate = re.search(r"(?is)\bWHERE\s+(.+)", sql[0] or "") if sql else None
        if (
            not partial
            or index_columns != [(column, "BINARY") for column in columns]
            or not predicate
            or not re.fullmatch(
                r"""\s*\(*\s*(?i:status|"status"|`status`|\[status\])\s*==?\s*'ACTIVE'\s*\)*\s*;?\s*""",
                predicate[1],
            )
        ):
            raise MigrationError(f"Incompatible active binding index: {expected_name}")


def init_database(db_path: Path | None = None) -> None:
    """Initialize or validate the authorization database in a single transaction."""
    path = DB_PATH if db_path is None else Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise MigrationError(f"Unsupported SQLite schema version {version}")

        tables = dict(conn.execute(
            "SELECT lower(name), type FROM sqlite_schema "
            "WHERE type IN ('table', 'view') AND name NOT GLOB 'sqlite_*'"
        ))
        known_tables = CURRENT_TABLES.keys() | LEGACY_TABLES
        if tables and not known_tables.intersection(tables):
            raise MigrationError("Database has no recognized MC-QQ-Auth tables")
        if any(tables[name] != "table" for name in known_tables.intersection(tables)):
            raise MigrationError("An expected MC-QQ-Auth table is a view")

        if version == 0:
            if "offline_bindings" in tables and "auth_state" not in tables:
                raise MigrationError("auth_state is missing for an existing offline_bindings table")
            _apply_initial_schema(conn)
            if "auth_state" not in tables:
                conn.execute("INSERT INTO auth_state (id, version) VALUES (1, 0)")

        for table in CURRENT_TABLES:
            _validate_table(conn, table)
        _validate_auth_state(conn)
        _validate_offline_bindings(conn)

        if version == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


if __name__ == "__main__":
    init_database()
    print("SQLite 数据库初始化完成")
