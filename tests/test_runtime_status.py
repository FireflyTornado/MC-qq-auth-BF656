import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import database
import notify_client
import runtime_status
from status_image import HEIGHT, WIDTH, render_status


def fields(status):
    return {label: value for _, section in status.sections for label, value in section}


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "private # auth.db"
        self.enterContext(patch.object(database, "DB_PATH", self.path))
        self.enterContext(patch.object(runtime_status, "_outcomes", {}))

    def test_collects_read_only_group_metrics_and_renders_png(self):
        database.init_database(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE auth_state SET version = 1800000000001")
            conn.executemany(
                """
                INSERT INTO offline_bindings
                    (group_id, qq_id, mc_name, mc_name_key, status, bound_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                """,
                [
                    ("1", "10", "Steve", "steve", "ACTIVE"),
                    ("1", "11", "Alex", "alex", "REVOKED"),
                    ("2", "12", "Other", "other", "ACTIVE"),
                ],
            )
        runtime_status.record_outcome("reconcile", "OK, 1 revoked")
        runtime_status.record_outcome("notify", "Accepted")
        gib = 1024 ** 3
        psutil = runtime_status.psutil
        with (
            patch.object(runtime_status, "check_fastapi", return_value=runtime_status.HealthResult(200, 24)),
            patch.object(psutil, "boot_time", return_value=900),
            patch.object(psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 950)),
            patch.object(psutil, "cpu_count", return_value=8),
            patch.object(psutil, "virtual_memory", return_value=SimpleNamespace(total=8*gib, available=6*gib, percent=25)),
            patch.object(psutil, "disk_usage", return_value=SimpleNamespace(total=100*gib, used=50*gib, percent=50)),
            patch.object(runtime_status.time, "time", return_value=1000),
        ):
            status = runtime_status.collect_status("1", connected=True, connected_for=25)
        data = fields(status)
        self.assertEqual(
            {key: data[key] for key in ("Bot", "OneBot", "FastAPI", "SQLite")},
            {"Bot": "Running", "OneBot": "Connected", "FastAPI": "HTTP 200", "SQLite": "Readable"},
        )
        self.assertEqual(data["System"], "0d 00h 01m 40s")
        self.assertEqual(data["Bot process"], "0d 00h 00m 50s")
        self.assertEqual(data["OneBot connection"], "0d 00h 00m 25s")
        self.assertEqual(data["Logical CPU cores"], "8")
        self.assertEqual(data["Memory usage"], "2.0 / 8.0 GiB (25%)")
        self.assertEqual(data["Disk usage"], "50.0 / 100.0 GiB (50%)")
        self.assertEqual(data["Authorization version"], "1800000000001")
        self.assertEqual(data["Active bindings"], "1")
        self.assertTrue(data["Last member reconciliation"].startswith("OK, 1 revoked"))
        self.assertTrue(data["Last authorization notice"].startswith("Accepted"))
        self.assertEqual(status.checked_at.tzinfo, UTC)
        with Image.open(BytesIO(render_status(status))) as image:
            self.assertEqual((image.format, image.size), ("PNG", (WIDTH, HEIGHT)))
            image.verify()

    def test_unavailable_metrics_do_not_leak_errors_or_prevent_png(self):
        secret = "secret-token db-path=127.0.0.1"
        with (
            patch.object(runtime_status, "check_fastapi", side_effect=OSError(secret)),
            patch.object(runtime_status.psutil, "virtual_memory", side_effect=OSError(secret)),
            patch.object(runtime_status.psutil, "boot_time", side_effect=OSError(secret)),
        ):
            status = runtime_status.collect_status("1", connected=False, connected_for=None)
        data = fields(status)
        for field in ("FastAPI", "SQLite", "System", "Memory usage", "OneBot connection", "Authorization version"):
            self.assertEqual(data[field], "Unavailable")
        self.assertEqual(data["OneBot"], "Disconnected")
        self.assertEqual(data["Last authorization notice"], "No attempts yet")
        self.assertNotIn(secret, repr(data))
        self.assertNotIn(str(self.path), repr(data))
        self.assertFalse(self.path.exists())
        with Image.open(BytesIO(render_status(status))) as image:
            image.verify()

    def test_health_returns_http_status_and_targets_only_loopback(self):
        with patch.object(runtime_status, "HTTPConnection") as http:
            connection = http.return_value
            connection.getresponse.return_value.__enter__.return_value.status = 200
            result = runtime_status.check_fastapi()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.summary, "HTTP 200")
        self.assertGreaterEqual(result.latency_ms, 0)
        http.assert_called_once_with("127.0.0.1", 18765, timeout=2)
        connection.request.assert_called_once_with("GET", "/internal/health")
        connection.close.assert_called_once_with()

    def test_health_sanitizes_timeout_and_connection_errors(self):
        for exc, reason in ((TimeoutError, "Timeout"), (ConnectionRefusedError, "Connection failed")):
            with self.subTest(exc=exc), patch.object(runtime_status, "HTTPConnection") as http:
                http.return_value.request.side_effect = exc("private address and token")
                result = runtime_status.check_fastapi()
                self.assertIsNone(result.status_code)
                self.assertEqual(result.summary, reason)
                http.return_value.close.assert_called_once_with()

    def test_notification_history_tracks_acknowledgement_and_failure(self):
        config = {"MC_NOTIFY_TOKEN": "test-only.status-token"}
        with (
            patch.object(notify_client, "load_config", return_value=config),
            patch.object(notify_client.opener, "open") as send,
        ):
            response = send.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"success":true,"version":1,"delivered":0}'
            self.assertTrue(notify_client.notify_auth_updated())
            self.assertEqual(runtime_status._outcomes["notify"][0], "Accepted")
            send.side_effect = URLError(config["MC_NOTIFY_TOKEN"])
            self.assertFalse(notify_client.notify_auth_updated())
            self.assertEqual(runtime_status._outcomes["notify"][0], "Request failed")
