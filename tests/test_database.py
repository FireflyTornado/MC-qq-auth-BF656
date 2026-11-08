import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import database

# Keep the database tests independent of dotenv, the bot framework and the network.
fake_notify = ModuleType("notify_client")
fake_notify.notify_auth_updated = Mock()
with patch.dict(sys.modules, {"notify_client": fake_notify}):
    import binding_service


AUTH_TABLE = """
CREATE TABLE auth_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
"""
OFFLINE_TABLE = """
CREATE TABLE offline_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    qq_id TEXT NOT NULL,
    mc_name TEXT NOT NULL,
    mc_name_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REVOKED')),
    bound_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""
ACTIVE_INDEXES = """
CREATE UNIQUE INDEX idx_offline_active_qq
ON offline_bindings (group_id, qq_id) WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX idx_offline_active_name
ON offline_bindings (mc_name_key) WHERE status = 'ACTIVE';
"""
BINDING_INSERT = """
INSERT INTO offline_bindings (group_id, qq_id, mc_name, mc_name_key, status, bound_at, updated_at)
VALUES (?, ?, ?, ?, ?, 100, 100)
"""


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = Path(temp_dir.name) / "auth.db"
        for module in (database, binding_service):
            patcher = patch.object(module, "DB_PATH", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)
        notify = patch.object(binding_service, "notify_auth_updated", return_value=False)
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(sql, params).fetchall()

    def fixture(self, sql):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(sql)

    def auth_version(self):
        return self.rows("SELECT version FROM auth_state WHERE id = 1")[0][0]

    def schema_version(self):
        return self.rows("PRAGMA user_version")[0][0]

    def test_initialize_new_database_and_repeat_after_binding(self):
        database.init_database()
        self.assertEqual(self.schema_version(), 1)
        self.assertEqual(self.auth_version(), 0)
        self.assertEqual(self.rows("SELECT * FROM offline_bindings"), [])
        self.assertEqual(
            self.rows("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"),
            [("auth_state",), ("offline_bindings",)],
        )
        self.assertEqual(binding_service.bind_player("group", "qq", "MiXeD"), ("success", "MiXeD"))
        version = self.auth_version()
        bindings = self.rows("SELECT * FROM offline_bindings")
        database.init_database()
        self.assertEqual(self.auth_version(), version)
        self.assertEqual(self.rows("SELECT * FROM offline_bindings"), bindings)

    def test_upgrade_legacy_uuid_and_verification_tables(self):
        self.fixture("""
            CREATE TABLE bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, qq_id TEXT NOT NULL, mc_uuid TEXT NOT NULL,
                mc_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
                bound_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE pending_bindings (
                request_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL, qq_id TEXT NOT NULL, mc_uuid TEXT NOT NULL,
                mc_name TEXT NOT NULL, code_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING', created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
            );
            INSERT INTO bindings VALUES (7, 'group', 'old-qq', 'fake-uuid', 'OldName', 'ACTIVE', 1, 2);
            INSERT INTO pending_bindings VALUES (
                'request', 'group', 'pending-qq', 'other-uuid', 'PendingName', 'fake-hash', 2, 'PENDING', 1, 5
            );
        """)
        bound = self.rows("SELECT * FROM bindings")
        pending = self.rows("SELECT * FROM pending_bindings")

        database.init_database()
        database.init_database()

        self.assertEqual(self.schema_version(), 1)
        self.assertEqual(self.auth_version(), 0)
        self.assertEqual(self.rows("SELECT * FROM bindings"), bound)
        self.assertEqual(self.rows("SELECT * FROM pending_bindings"), pending)
        self.assertEqual(self.rows("SELECT * FROM offline_bindings"), [])

    def test_keep_existing_offline_rows_indexes_and_version(self):
        self.fixture(AUTH_TABLE + OFFLINE_TABLE + ACTIVE_INDEXES)
        self.rows("INSERT INTO auth_state VALUES (1, ?)", (1_800_000_000_000,))
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "ACTIVE"))
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "REVOKED"))
        existing = self.rows("SELECT * FROM offline_bindings ORDER BY id")

        database.init_database()
        database.init_database()

        self.assertEqual(self.auth_version(), 1_800_000_000_000)
        self.assertEqual(self.rows("SELECT * FROM offline_bindings ORDER BY id"), existing)
        self.assertEqual(binding_service.get_binding("group", "qq"), "MyName")
        self.assertEqual(binding_service.bind_player("group", "qq", "Other"), ("already_bound", "MyName"))
        self.assertEqual(binding_service.bind_player("other", "new-qq", "MYNAME"), ("name_taken", None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.rows(BINDING_INSERT, ("group", "qq", "Other", "other", "ACTIVE"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.rows(BINDING_INSERT, ("other", "new-qq", "MyName", "myname", "ACTIVE"))

    def test_unversioned_legacy_auth_version_is_preserved(self):
        self.fixture(AUTH_TABLE + "INSERT INTO auth_state VALUES (1, 42);")
        database.init_database()
        self.assertEqual(self.auth_version(), 42)
        self.assertEqual(self.schema_version(), 1)

    def test_compatible_unversioned_table_without_indexes_can_be_upgraded(self):
        self.fixture(AUTH_TABLE + OFFLINE_TABLE + "INSERT INTO auth_state VALUES (1, 10);")
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "ACTIVE"))
        database.init_database()
        self.assertEqual(self.auth_version(), 10)
        self.assertEqual(binding_service.get_binding("group", "qq"), "MyName")
        self.assertEqual(len(self.rows("PRAGMA index_list(offline_bindings)")), 2)

    def test_duplicate_active_name_rolls_back_first_index_creation(self):
        self.fixture(AUTH_TABLE + OFFLINE_TABLE + "INSERT INTO auth_state VALUES (1, 42);")
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "ACTIVE"))
        self.rows(BINDING_INSERT, ("other", "new-qq", "MYNAME", "myname", "ACTIVE"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, r"offline_bindings\.mc_name_key"):
            database.init_database()
        self.assertEqual(self.schema_version(), 0)
        self.assertEqual(self.auth_version(), 42)
        self.assertEqual(self.rows("PRAGMA index_list(offline_bindings)"), [])
        self.assertEqual(self.rows("SELECT count(*) FROM offline_bindings"), [(2,)])

    def test_incompatible_index_predicate_is_rejected(self):
        self.fixture(
            AUTH_TABLE + OFFLINE_TABLE + "INSERT INTO auth_state VALUES (1, 42);"
            + """CREATE UNIQUE INDEX idx_offline_active_name ON offline_bindings (mc_name_key)
                 WHERE status = 'REVOKED';"""
        )
        with self.assertRaisesRegex(database.MigrationError, "idx_offline_active_name"):
            database.init_database()
        self.assertEqual(self.schema_version(), 0)
        self.assertEqual([row[1] for row in self.rows("PRAGMA index_list(offline_bindings)")], [
            "idx_offline_active_name",
        ])

    def test_invalid_lowercase_key_is_not_rewritten(self):
        self.fixture(AUTH_TABLE + OFFLINE_TABLE + "INSERT INTO auth_state VALUES (1, 42);")
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "MyName", "ACTIVE"))
        with self.assertRaisesRegex(database.MigrationError, "lowercase key"):
            database.init_database()
        self.assertEqual(self.rows("SELECT mc_name_key FROM offline_bindings"), [("MyName",)])
        self.assertEqual(self.rows("PRAGMA index_list(offline_bindings)"), [])
        self.assertEqual(self.schema_version(), 0)

    def test_incompatible_columns_roll_back_offline_table_creation(self):
        self.fixture("CREATE TABLE auth_state (id INTEGER PRIMARY KEY, version TEXT NOT NULL);")
        with self.assertRaisesRegex(database.MigrationError, "Incompatible columns"):
            database.init_database()
        self.assertEqual(self.rows("SELECT name FROM sqlite_schema WHERE type = 'table'"), [("auth_state",)])
        self.assertEqual(self.schema_version(), 0)

    def test_incomplete_current_schema_is_not_silently_repaired(self):
        database.init_database()
        self.rows("DROP INDEX idx_offline_active_qq")
        with self.assertRaisesRegex(database.MigrationError, "unique index"):
            database.init_database()
        self.assertEqual(len(self.rows("PRAGMA index_list(offline_bindings)")), 1)
        self.assertEqual(self.schema_version(), 1)

    def test_offline_table_without_auth_state_does_not_seed_a_version(self):
        self.fixture(OFFLINE_TABLE)
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "ACTIVE"))
        with self.assertRaisesRegex(database.MigrationError, "auth_state is missing"):
            database.init_database()
        self.assertEqual(
            self.rows("SELECT name FROM sqlite_schema WHERE name = 'auth_state'"), []
        )
        self.assertEqual(self.rows("SELECT mc_name FROM offline_bindings"), [("MyName",)])
        self.assertEqual(self.schema_version(), 0)

    def test_missing_auth_row_is_not_seeded_in_existing_auth_table(self):
        self.fixture(AUTH_TABLE)
        with self.assertRaisesRegex(database.MigrationError, "exactly one row"):
            database.init_database()
        self.assertEqual(self.rows("SELECT * FROM auth_state"), [])
        self.assertEqual(self.rows("SELECT name FROM sqlite_schema WHERE name = 'offline_bindings'"), [])
        self.assertEqual(self.schema_version(), 0)

    def test_invalid_auth_version_is_not_reset(self):
        self.fixture(AUTH_TABLE + "INSERT INTO auth_state VALUES (1, 'broken');")
        with self.assertRaisesRegex(database.MigrationError, "nonnegative integer"):
            database.init_database()
        self.assertEqual(self.auth_version(), "broken")
        self.assertEqual(self.schema_version(), 0)

    def test_unsupported_version_is_not_changed(self):
        self.fixture("PRAGMA user_version = 2;")
        with self.assertRaisesRegex(database.MigrationError, "Unsupported SQLite schema version 2"):
            database.init_database()
        self.assertEqual(self.schema_version(), 2)
        self.assertEqual(self.rows("SELECT name FROM sqlite_schema"), [])

    def test_bind_and_unbind_are_monotonic_and_notify_after_commit(self):
        database.init_database()
        observed = []

        def observe_commit():
            observed.append((self.auth_version(), binding_service.get_binding("group", "qq")))
            return False

        self.notify.side_effect = observe_commit
        now = 1_800_000_000_000
        with patch.object(binding_service.time, "time_ns", side_effect=[
            now * 1_000_000, now * 1_000_000, (now - 100) * 1_000_000,
        ]):
            self.assertEqual(binding_service.bind_player("group", "qq", "MyName"), ("success", "MyName"))
            self.assertEqual(binding_service.unbind_player("group", "qq"), "MyName")
            self.assertEqual(binding_service.bind_player("group", "qq", "MyName"), ("success", "MyName"))
        self.assertEqual(observed, [(now, "MyName"), (now + 1, None), (now + 2, "MyName")])

    def test_bind_and_unbind_roll_back_without_auth_row(self):
        database.init_database()
        self.rows(BINDING_INSERT, ("group", "qq", "MyName", "myname", "ACTIVE"))
        self.rows("DELETE FROM auth_state")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "授权版本"):
            binding_service.bind_player("group", "other-qq", "Other")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "授权版本"):
            binding_service.unbind_player("group", "qq")
        self.assertEqual(self.rows("SELECT mc_name FROM offline_bindings"), [("MyName",)])
        self.notify.assert_not_called()

    def test_version_overflow_cannot_commit_binding(self):
        database.init_database()
        self.rows("UPDATE auth_state SET version = ?", (database.MAX_AUTH_VERSION,))
        with self.assertRaises(sqlite3.IntegrityError):
            binding_service.bind_player("group", "qq", "MyName")
        self.assertEqual(self.auth_version(), database.MAX_AUTH_VERSION)
        self.assertEqual(self.rows("SELECT * FROM offline_bindings"), [])
        self.notify.assert_not_called()

    def test_concurrent_initialization_and_name_claim(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(database.init_database)
            second = pool.submit(database.init_database)
            first.result()
            second.result()

            first = pool.submit(binding_service.bind_player, "group", "qq1", "MyName")
            second = pool.submit(binding_service.bind_player, "group", "qq2", "MYNAME")
            self.assertCountEqual([first.result()[0], second.result()[0]], ["success", "name_taken"])

        self.assertEqual(self.schema_version(), 1)
        self.assertEqual(self.rows("SELECT mc_name_key FROM offline_bindings"), [("myname",)])
        self.notify.assert_called_once()

    def test_cli_explicit_path_and_nonzero_exit_for_invalid_version(self):
        command = [sys.executable, "-B", str(BOT_DIR / "migrate.py"), "--db", str(self.path)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("schema 1", result.stdout)
        self.fixture("PRAGMA user_version = 2;")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unsupported SQLite schema version 2", result.stderr)
