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
from xml.etree import ElementTree

from PIL import Image

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
        self.enterContext(patch.object(runtime_status, "load_config", return_value={}))

    def rendered_svg(self, status, **identity):
        with patch.object(status_image, "svg_to_bytes", wraps=status_image.svg_to_bytes) as rasterize:
            with Image.open(BytesIO(render_status(status, **identity))) as image:
                self.assertEqual((image.format, image.size), ("PNG", (WIDTH, HEIGHT)))
                image.verify()
        svg = ElementTree.fromstring(rasterize.call_args.kwargs["svg_string"])
        return {element.attrib["id"]: element for element in svg.iter() if "id" in element.attrib}

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
        nodes = self.rendered_svg(status)
        self.assertEqual(nodes["bindings-count"].text, "—")
        self.assertTrue(nodes["system-uptime"].text.endswith("—"))

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

    def test_health_reads_optional_connection_count_without_changing_http_result(self):
        for body, expected in (
            (b'{"status":"ok","minecraft_connections":1}', 1),
            (b'{"status":"ok","minecraft_connections":0}', 0),
            (b'{"status":"ok"}', None),
            (b'{"minecraft_connections":true}', None),
            (b'broken', None),
        ):
            with self.subTest(body=body), patch.object(runtime_status, "HTTPConnection") as http:
                response = http.return_value.getresponse.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = body
                result = runtime_status.check_fastapi(include_connections=True)
                self.assertEqual((result.status_code, result.minecraft_connections), (200, expected))

    def test_minecraft_probe_reports_unconfigured_reachable_and_unresponsive(self):
        address = "play.example.org:25565"
        with (
            patch.object(runtime_status, "load_config", return_value={}) as config,
            patch.object(runtime_status.JavaServer, "lookup") as lookup,
        ):
            self.assertIsNone(runtime_status.check_minecraft())
            lookup.assert_not_called()

            config.return_value = {"MC_STATUS_ADDRESS": address}
            probe = lookup.return_value.status
            probe.return_value = SimpleNamespace(players=SimpleNamespace(online=0, max=30))
            self.assertEqual(runtime_status.check_minecraft(), runtime_status.MinecraftStatus(True, 0, 30))
            lookup.assert_called_once_with(address, timeout=runtime_status.MINECRAFT_TIMEOUT)
            probe.assert_called_once_with(tries=1)

            probe.side_effect = TimeoutError("private address")
            self.assertEqual(runtime_status.check_minecraft(), runtime_status.MinecraftStatus(False))

    def test_custom_title_label_and_long_nickname_fit_and_footer_is_local_time_only(self):
        path = self.path.with_name("messages.yml")
        long_text = ("很长的状态名称" if status_image._font_source(False) else "Long title ") * 80
        qq_id = "12345678901234567890"
        path.write_text(
            f'info:\n  title: "{long_text}"\n  footer: "legacy footer"\n  labels:\n'
            f'    database: "Auth DB"\n    api: "{long_text}"\n    onebot: [invalid]\n    bot_uptime: "Bot 进程"\n',
            encoding="utf-8",
        )
        messages = message_config.load_messages(path)
        status = runtime_status.RuntimeStatus(
            checked_at=LocalClock(2026, 11, 9, 7, 30, tzinfo=UTC), connected=True, active_bindings=0,
        )
        with patch.object(message_config, "MESSAGES", messages):
            nodes = self.rendered_svg(status, qq_id=qq_id, nickname=long_text)
        self.assertEqual(nodes["sqlite-name"].text, "Auth DB")
        self.assertEqual(nodes["onebot-name"].text, "OneBot")
        self.assertEqual(nodes["bot-name"].text, "NoneBot2")
        self.assertEqual(nodes["qq-id"].text, f"QQ {qq_id}")
        self.assertEqual(nodes["bindings-count"].text, "0")
        self.assertEqual(nodes["bindings-count"].get("font-size"), "47")
        self.assertEqual(nodes["timestamp"].text, "2026-11-09 15:30 UTC+08:00")
        for key in ("title", "nickname", "api-name"):
            with self.subTest(key=key):
                self.assertTrue(nodes[key].text.endswith("…"))

    def test_render_escapes_nickname_and_clips_avatar_in_png(self):
        status = runtime_status.RuntimeStatus(datetime.now(UTC))
        avatar = Image.new("RGB", (35, 50), "#e14430")
        nickname = '<image id="injected"/> & name'
        nodes = self.rendered_svg(status, nickname=nickname, avatar=avatar)
        self.assertEqual(nodes["nickname"].text, nickname)
        self.assertNotIn("injected", nodes)
        with Image.open(BytesIO(render_status(status, avatar=avatar))) as image:
            self.assertEqual(image.getpixel((76, 120))[:3], (225, 68, 48))
            self.assertNotEqual(image.getpixel((49, 93))[:3], (225, 68, 48))

    def test_render_maps_usage_and_service_states(self):
        gib = 1024 ** 3
        cjk = status_image._font_source(False) is not None
        for connected, api, sqlite, expected, detail in (
            (True, runtime_status.HealthResult(200, 7), True, ("ok", "ok", "ok"), "7 ms"),
            (False, runtime_status.HealthResult(503, 7), False, ("error", "warning", "error"), "HTTP 503"),
            (None, runtime_status.HealthResult(None, 7, "Connection failed"), None,
             ("unknown", "error", "unknown"), "连接失败" if cjk else "Unreachable"),
            (None, runtime_status.HealthResult(None, 7, "Timeout"), None,
             ("unknown", "warning", "unknown"), "超时" if cjk else "Timeout"),
            (None, None, None, ("unknown", "unknown", "unknown"), None),
        ):
            status = runtime_status.RuntimeStatus(
                checked_at=datetime.now(UTC), connected=connected, api=api, sqlite_readable=sqlite,
                cpu_percent=12, memory=runtime_status.ResourceUsage(2*gib, 8*gib, 25),
                disk=runtime_status.ResourceUsage(20*gib, 80*gib, 25),
            )
            with self.subTest(expected=expected):
                nodes = self.rendered_svg(status)
                colors = {"ok": status_image.MINT, "warning": status_image.YELLOW,
                          "error": status_image.RED, "unknown": status_image.GRAY}
                dashes = {"ok": "none", "warning": "8 3", "error": "5 4", "unknown": "2 4"}
                for key, state in zip(("onebot-link", "api-link", "sqlite-link"), expected):
                    self.assertEqual(nodes[key].get("stroke"), colors[state])
                    self.assertEqual(nodes[key][0].get("stroke-dasharray"), dashes[state])
                self.assertEqual(nodes["api-status"].text, detail)
                self.assertEqual(nodes["cpu-value"].text, "12%")
                self.assertEqual(nodes["memory-value"].text, "25%")
                self.assertEqual(nodes["disk-value"].text, "25%")
                self.assertEqual(nodes["memory-detail"].text, "2.0 / 8.0 GiB")
                self.assertEqual(nodes["disk-detail"].text, "20.0 / 80.0 GiB")

    def test_render_separates_wss_state_from_game_reachability_and_shows_uptimes(self):
        status = runtime_status.RuntimeStatus(
            checked_at=datetime.now(UTC), connected=True, api=runtime_status.HealthResult(200, 12, minecraft_connections=0),
            system_seconds=280800, bot_seconds=86400, connection_seconds=7200,
            minecraft=runtime_status.MinecraftStatus(True, 8, 30),
        )
        cjk = status_image._font_source(False) is not None
        nodes = self.rendered_svg(status)
        self.assertEqual(nodes["bot-name"].text, "NoneBot2")
        self.assertEqual(nodes["onebot-uptime"].text, "2小时 0分" if cjk else "2h 0m")
        self.assertEqual(nodes["bot-uptime"].text, "1天 0小时" if cjk else "1d 0h")
        self.assertIn("3天 6小时" if cjk else "3d 6h", nodes["system-uptime"].text)
        self.assertEqual(nodes["api-status"].text, "12 ms")
        self.assertEqual(nodes["minecraft-players"].text, "8/30")
        self.assertEqual(nodes["sqlite-name"].text, "SQLite")
        self.assertEqual(nodes["wss-link"].get("stroke"), status_image.RED)
        self.assertEqual(nodes["wss-link"][0].get("stroke-dasharray"), "5 4")
        self.assertEqual(nodes["minecraft-node"].get("stroke"), status_image.MINT)
        self.assertEqual([element.tag.rsplit("}", 1)[-1] for element in nodes["wss-link"]], ["path", "text"])

    def test_missing_cjk_font_warns_and_renders_fallbacks(self):
        find_font = status_image._find_font

        def missing(candidates):
            return None if "NotoSansCJK" in candidates[0][0] else find_font(candidates)

        status_image._font_source.cache_clear()
        self.addCleanup(status_image._font_source.cache_clear)
        with (
            patch.object(status_image, "_find_font", side_effect=missing),
            patch.object(status_image.logger, "warning") as warning,
        ):
            nodes = self.rendered_svg(runtime_status.RuntimeStatus(datetime.now(UTC)), nickname="中文昵称")
        warning.assert_called_once()
        self.assertEqual(nodes["nickname"].text, "QQ Bot")
        self.assertEqual(nodes["resources-label"].text, "System resources")

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
