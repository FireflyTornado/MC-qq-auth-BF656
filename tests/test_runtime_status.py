import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import database
import message_config
import notify_client
import runtime_status
import status_image
from status_image import HEIGHT, WIDTH, render_status


class LocalClock(datetime):
    def astimezone(self, tz=None):
        return super().astimezone(tz or timezone(timedelta(hours=8)))


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "private # auth.db"
        self.enterContext(patch.object(database, "DB_PATH", self.path))
        self.enterContext(patch.object(runtime_status, "_outcomes", {}))

    def rendered_text(self, status, **identity):
        boxes = {}
        draw_text = ImageDraw.ImageDraw.text

        def capture(draw, position, text, **options):
            scale = status_image.SCALE
            box = draw.textbbox(position, text, font=options["font"], anchor=options.get("anchor"))
            boxes[tuple(value / scale for value in position)] = (text, tuple(value / scale for value in box))
            return draw_text(draw, position, text, **options)

        with patch.object(ImageDraw.ImageDraw, "text", autospec=True, side_effect=capture):
            with Image.open(BytesIO(render_status(status, **identity))) as image:
                self.assertEqual((image.format, image.size), ("PNG", (WIDTH, HEIGHT)))
                image.verify()
        return boxes

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
            patch.object(psutil, "cpu_percent", return_value=12) as cpu,
            patch.object(psutil, "virtual_memory", return_value=SimpleNamespace(total=8*gib, available=6*gib, percent=25)),
            patch.object(psutil, "disk_usage", return_value=SimpleNamespace(total=100*gib, used=50*gib, percent=50)),
            patch.object(runtime_status.time, "time", return_value=1000),
            patch.object(runtime_status.platform, "system", return_value="Linux"),
            patch.object(runtime_status.platform, "freedesktop_os_release",
                         return_value={"NAME": "Ubuntu", "VERSION_ID": "24.04"}),
        ):
            status = runtime_status.collect_status("1", connected=True, connected_for=25)
        cpu.assert_called_once_with(interval=runtime_status.CPU_SAMPLE_INTERVAL)
        self.assertGreater(runtime_status.CPU_SAMPLE_INTERVAL, 0)
        self.assertTrue(status.connected)
        self.assertTrue(status.sqlite_readable)
        self.assertEqual(status.api, runtime_status.HealthResult(200, 24))
        self.assertEqual(status.system_name, "Ubuntu 24.04")
        self.assertEqual((status.system_seconds, status.bot_seconds, status.connection_seconds), (100, 50, 25))
        self.assertEqual((status.cpu_cores, status.cpu_percent), (8, 12))
        self.assertEqual(status.memory, runtime_status.ResourceUsage(2*gib, 8*gib, 25))
        self.assertEqual(status.disk, runtime_status.ResourceUsage(50*gib, 100*gib, 50))
        self.assertEqual(status.auth_version, 1800000000001)
        self.assertEqual(status.active_bindings, 1)
        self.assertTrue(status.last_reconcile.startswith("OK, 1 revoked"))
        self.assertTrue(status.last_notice.startswith("Accepted"))
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
            patch.object(runtime_status.psutil, "cpu_percent", side_effect=OSError(secret)),
        ):
            status = runtime_status.collect_status("1", connected=False, connected_for=None)
        for field in ("api", "system_seconds", "memory", "cpu_percent", "connection_seconds",
                      "auth_version", "active_bindings"):
            self.assertIsNone(getattr(status, field))
        self.assertFalse(status.connected)
        self.assertFalse(status.sqlite_readable)
        self.assertEqual(status.last_notice, "No attempts yet")
        self.assertNotIn(secret, repr(status))
        self.assertNotIn(str(self.path), repr(status))
        self.assertFalse(self.path.exists())
        boxes = self.rendered_text(status)
        self.assertEqual(boxes[130, 449][0], "—")
        self.assertEqual(boxes[652, 467][0], "—")

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

    def test_custom_title_label_and_long_nickname_fit_and_footer_is_local_time_only(self):
        path = self.path.with_name("messages.yml")
        long_text = ("很长的状态名称" if status_image._font_source(False) else "Long title ") * 80
        qq_id = "12345678901234567890"
        path.write_text(
            f'info:\n  title: "{long_text}"\n  footer: "legacy footer"\n  labels:\n'
            f'    database: "Auth DB"\n    api: "{long_text}"\n    onebot: [invalid]\n',
            encoding="utf-8",
        )
        messages = message_config.load_messages(path)
        status = runtime_status.RuntimeStatus(
            checked_at=LocalClock(2026, 11, 9, 7, 30, tzinfo=UTC), connected=True, active_bindings=0,
        )
        with patch.object(message_config, "MESSAGES", messages):
            boxes = self.rendered_text(status, qq_id=qq_id, nickname=long_text)
        self.assertEqual(boxes[724, 146][0], "Auth DB")
        self.assertEqual(boxes[432, 146][0], "OneBot")
        self.assertEqual(boxes[131, 151][0], f"QQ {qq_id}")
        self.assertEqual(boxes[130, 449][0], "0")
        self.assertEqual(
            {text for (_, y), (text, _) in boxes.items() if y > 540},
            {"2026-11-09 15:30 UTC+08:00"},
        )
        for position, right, bottom in (
            ((74, 39), 720, 76),
            ((131, 119), 353, 142),
            ((578, 146), 685, 166),
        ):
            with self.subTest(position=position):
                rendered, box = boxes[position]
                self.assertTrue(rendered.endswith("…"))
                self.assertLessEqual(box[2], right + 1)
                self.assertLessEqual(box[3], bottom)

    def test_render_maps_usage_and_service_states(self):
        gib = 1024 ** 3
        for connected, api, sqlite, expected in (
            (True, runtime_status.HealthResult(200, 7), True, ("ok", "ok", "ok")),
            (False, runtime_status.HealthResult(503, 7), False, ("error", "warning", "error")),
            (None, runtime_status.HealthResult(None, 7, "Connection failed"), None, ("unknown", "error", "unknown")),
            (None, runtime_status.HealthResult(None, 7, "Timeout"), None, ("unknown", "warning", "unknown")),
            (None, None, None, ("unknown", "unknown", "unknown")),
        ):
            status = runtime_status.RuntimeStatus(
                checked_at=datetime.now(UTC), connected=connected, api=api, sqlite_readable=sqlite,
                cpu_percent=12, memory=runtime_status.ResourceUsage(2*gib, 8*gib, 25),
                disk=runtime_status.ResourceUsage(20*gib, 80*gib, 25),
            )
            with self.subTest(expected=expected), patch.object(status_image._Canvas, "light", autospec=True) as light:
                boxes = self.rendered_text(status)
            colors = {"ok": status_image.MINT, "warning": status_image.YELLOW,
                      "error": status_image.RED, "unknown": status_image.GRAY}
            self.assertEqual([call.args[-1] for call in light.call_args_list[1:]], [colors[key] for key in expected])
            self.assertEqual(boxes[86, 321][0], "12%")
            self.assertEqual(boxes[362, 321][0], "25%")
            self.assertEqual(boxes[836, 287][0], "25%")
            self.assertEqual(boxes[424, 337][0], "2.0 / 8.0 GiB")
            self.assertEqual(boxes[597, 324][0], "20.0 / 80.0 GiB")

    def test_missing_cjk_font_warns_and_renders_fallbacks(self):
        truetype = status_image.ImageFont.truetype

        def missing(font, *args, **kwargs):
            if isinstance(font, str):
                raise OSError
            return truetype(font, *args, **kwargs)

        status_image._font_source.cache_clear()
        self.addCleanup(status_image._font_source.cache_clear)
        with (
            patch.object(status_image.ImageFont, "truetype", side_effect=missing),
            patch.object(status_image.logger, "warning") as warning,
        ):
            boxes = self.rendered_text(runtime_status.RuntimeStatus(datetime.now(UTC)), nickname="中文昵称")
        warning.assert_called_once()
        self.assertEqual(boxes[131, 119][0], "QQ Bot")
        self.assertEqual(boxes[44, 226][0], "System resources")

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
