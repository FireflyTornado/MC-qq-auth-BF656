import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app_config
import binding_service
import database
import notify_client

API_TOKEN = "test-only.api~token+="
NOTIFY_TOKEN = "test-only.notify-token"
GROUP_ID = "12345"
VERSION = 1_800_000_000_001
ENV = {
    "MC_API_TOKEN": API_TOKEN,
    "MC_NOTIFY_TOKEN": NOTIFY_TOKEN,
    "TARGET_GROUP_ID": GROUP_ID,
}
SNAPSHOT_PATH = "/internal/mc/snapshot"
NOTIFY_PATH = "/internal/mc/notify"
WS_PATH = "/internal/mc/ws"


def bearer(token):
    return f"Bearer {token}".encode("ascii")


def scope(kind, path, authorization):
    return {
        "type": kind,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws" if kind == "websocket" else "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"authorization", value) for value in authorization],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 18765),
    }


class ApiAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        db_path = root / "auth.db"
        database.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE auth_state SET version = ?", (VERSION,))
            conn.executemany(
                """
                INSERT INTO offline_bindings
                    (group_id, qq_id, mc_name, mc_name_key, status, bound_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                """,
                [
                    (GROUP_ID, "1", "Steve", "steve", "ACTIVE"),
                    (GROUP_ID, "2", "Alex", "alex", "ACTIVE"),
                    (GROUP_ID, "3", "Former", "former", "REVOKED"),
                    ("99999", "4", "Other", "other", "ACTIVE"),
                ],
            )
        env_file = root / ".env"
        env_file.write_text("TARGET_GROUP_ID=00" + GROUP_ID + "\n", encoding="utf-8")
        with patch.object(app_config, "ENV_FILE", env_file), patch.dict(
            os.environ, {"MC_API_TOKEN": API_TOKEN, "MC_NOTIFY_TOKEN": NOTIFY_TOKEN}, clear=True
        ):
            self.api = importlib.import_module("mc_api")
        self.addCleanup(sys.modules.pop, "mc_api", None)
        self.api.DB_PATH = db_path

    async def request(self, path, authorization=(), method="GET"):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await self.api.app(
            {**scope("http", path, authorization), "method": method}, receive, send
        )
        headers = dict(sent[0]["headers"])
        return sent[0]["status"], headers, json.loads(sent[-1]["body"])

    async def test_snapshot_keeps_contract_and_accepts_standard_bearer_syntax(self):
        for header in (bearer(API_TOKEN), f"\tbEaReR   {API_TOKEN} \t".encode("ascii")):
            with self.subTest(header_syntax=header.startswith(b"Bearer ")):
                status, headers, payload = await self.request(SNAPSHOT_PATH, [header])
                self.assertEqual(status, 200)
                self.assertEqual(headers[b"cache-control"], b"no-store")
                self.assertEqual(payload, {"version": VERSION, "active_names": ["alex", "steve"]})
                self.assertIsInstance(payload["version"], int)

    async def test_http_rejects_invalid_bearer_input(self):
        cases = {
            "missing": [],
            "wrong": [bearer("test-only.invalid")],
            "empty": [b"Bearer "],
            "non-ascii": [b"Bearer abc\xff"],
            "control": [b"Bearer abc\x00"],
            "long": [b"Bearer " + b"x" * (app_config.MAX_TOKEN_LENGTH + 1)],
            "wrong-scheme": [b"Basic abc"],
            "bad-separator": [b"Bearer\tabc"],
            "extra-credential": [b"Bearer abc, Bearer def"],
            "duplicate-header": [bearer(API_TOKEN), bearer(API_TOKEN)],
            "invalid-padding": [b"Bearer ab=c"],
        }
        for name, header in cases.items():
            with self.subTest(case=name):
                status, headers, payload = await self.request(SNAPSHOT_PATH, header)
                self.assertEqual(status, 401)
                self.assertEqual(headers[b"www-authenticate"], b"Bearer")
                self.assertEqual(payload, {"detail": "Unauthorized"})

    async def test_notify_and_api_tokens_are_not_interchangeable(self):
        self.assertEqual((await self.request(SNAPSHOT_PATH, [bearer(NOTIFY_TOKEN)]))[0], 401)
        self.assertEqual((await self.request(NOTIFY_PATH, [bearer(API_TOKEN)], "POST"))[0], 401)
        self.assertEqual((await self.request(NOTIFY_PATH, [b"Bearer x\xff"], "POST"))[0], 401)
        status, _, body = await self.request(NOTIFY_PATH, [bearer(NOTIFY_TOKEN)], "POST")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"success": True, "version": VERSION, "delivered": 0})

    async def test_websocket_denies_before_accept_and_registration(self):
        for name, header in (
            ("wrong", bearer("test-only.invalid")),
            ("non-ascii", b"Bearer abc\xff"),
            ("notify-token", bearer(NOTIFY_TOKEN)),
        ):
            async def receive():
                return {"type": "websocket.connect"}

            sent = []

            async def send(message):
                sent.append(message)

            with self.subTest(case=name):
                await asyncio.wait_for(
                    self.api.app(scope("websocket", WS_PATH, [header]), receive, send), timeout=2
                )
                self.assertEqual([message["type"] for message in sent], ["websocket.close"])
                self.assertEqual(self.api.connections, set())

    async def test_websocket_connects_and_broadcasts_existing_messages(self):
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        task = asyncio.create_task(
            self.api.app(
                scope("websocket", WS_PATH, [bearer(API_TOKEN)]), incoming.get, outgoing.put
            )
        )
        try:
            accepted = await asyncio.wait_for(outgoing.get(), timeout=2)
            self.assertEqual(accepted["type"], "websocket.accept")
            connected = await asyncio.wait_for(outgoing.get(), timeout=2)
            self.assertEqual(json.loads(connected["text"]), {
                "type": "connected", "message": "Minecraft authentication connected",
            })
            status, _, notify = await self.request(NOTIFY_PATH, [bearer(NOTIFY_TOKEN)], "POST")
            self.assertEqual(status, 200)
            self.assertEqual(notify, {"success": True, "version": VERSION, "delivered": 1})
            updated = await asyncio.wait_for(outgoing.get(), timeout=2)
            self.assertEqual(json.loads(updated["text"]), {"type": "auth_updated", "version": VERSION})
        finally:
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(self.api.connections, set())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            f"MC_API_TOKEN={API_TOKEN}\nMC_NOTIFY_TOKEN={NOTIFY_TOKEN}\nTARGET_GROUP_ID=00{GROUP_ID}\n"
            "ONEBOT_V11_ACCESS_TOKEN=test-only.file-onebot\n",
            encoding="utf-8",
        )
        path_patch = patch.object(app_config, "ENV_FILE", self.env_file)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def test_dotenv_and_explicit_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            config = app_config.load_config()
            self.assertEqual(app_config.require_token(config, "MC_API_TOKEN"), API_TOKEN)
            self.assertEqual(app_config.target_group_id(config), int(GROUP_ID))
            self.assertEqual(app_config.notify_url(config), app_config.DEFAULT_NOTIFY_URL)
            self.assertNotIn("MC_API_TOKEN", os.environ)

        with patch.dict(os.environ, {
            "MC_API_TOKEN": "test-only.override",
            "MC_NOTIFY_TOKEN": "",
            "ONEBOT_ACCESS_TOKEN": "test-only.environment-onebot",
        }, clear=True):
            config = app_config.load_config()
        self.assertEqual(app_config.require_token(config, "MC_API_TOKEN"), "test-only.override")
        self.assertEqual(config["ONEBOT_ACCESS_TOKEN"], "test-only.environment-onebot")
        with self.assertRaisesRegex(app_config.ConfigError, "MC_NOTIFY_TOKEN"):
            app_config.require_token(config, "MC_NOTIFY_TOKEN")

    def test_invalid_settings_report_keys_without_values(self):
        for token in ("tést-only", "test-only\nsecret", "REPLACE_WITH_MC_API_TOKEN"):
            with self.subTest(token_format=type(token).__name__):
                with self.assertRaisesRegex(app_config.ConfigError, "MC_API_TOKEN") as error:
                    app_config.require_token({"MC_API_TOKEN": token}, "MC_API_TOKEN")
                self.assertNotIn(token, str(error.exception))
        for group in ({}, {"TARGET_GROUP_ID": "0"}, {"TARGET_GROUP_ID": "１２３"}):
            with self.assertRaisesRegex(app_config.ConfigError, "TARGET_GROUP_ID"):
                app_config.target_group_id(group)
        with self.assertRaisesRegex(app_config.ConfigError, "ONEBOT_WS_URLS"):
            app_config.onebot_ws_urls({"ONEBOT_WS_URLS": "not-json"})
        with self.assertRaisesRegex(app_config.ConfigError, "MC_NOTIFY_URL") as error:
            app_config.notify_url({"MC_NOTIFY_URL": "http://user:test-only.password@127.0.0.1/"})
        self.assertNotIn("test-only.password", str(error.exception))

    def test_api_startup_rejects_missing_or_shared_credentials(self):
        cases = (
            ({"MC_API_TOKEN": ""}, "MC_API_TOKEN"),
            ({"MC_NOTIFY_TOKEN": ""}, "MC_NOTIFY_TOKEN"),
            ({"MC_NOTIFY_TOKEN": API_TOKEN}, "MC_API_TOKEN.*MC_NOTIFY_TOKEN"),
        )
        for override, keys in cases:
            with self.subTest(keys=keys), patch.dict(os.environ, override, clear=True):
                with self.assertRaisesRegex(app_config.ConfigError, keys) as error:
                    importlib.import_module("mc_api")
                self.assertNotIn(API_TOKEN, str(error.exception))

    def test_notify_url_override_and_direct_request(self):
        url = "http://127.0.0.1:19002/custom/notify"
        with patch.dict(os.environ, {"MC_NOTIFY_URL": url}, clear=True), patch.object(
            notify_client.opener, "open"
        ) as send:
            send.return_value.__enter__.return_value.status = 200
            self.assertTrue(notify_client.notify_auth_updated())
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {NOTIFY_TOKEN}")
        self.assertEqual(send.call_args.kwargs, {"timeout": 3})

    def test_network_failure_keeps_committed_binding_and_hides_error_values(self):
        path = self.root / "auth.db"
        database.init_database(path)
        with patch.dict(os.environ, {}, clear=True), patch.object(
            binding_service, "DB_PATH", path
        ), patch.object(
            notify_client.opener, "open", side_effect=URLError(NOTIFY_TOKEN)
        ), self.assertLogs("mc_qq_auth", level="WARNING") as logs:
            self.assertEqual(binding_service.bind_player(GROUP_ID, "qq", "MiXeD"), ("success", "MiXeD"))
            self.assertEqual(binding_service.get_binding(GROUP_ID, "qq"), "MiXeD")
        self.assertNotIn(NOTIFY_TOKEN, "\n".join(logs.output))
        with closing(sqlite3.connect(path)) as conn:
            self.assertGreater(conn.execute("SELECT version FROM auth_state WHERE id = 1").fetchone()[0], 0)

    def test_bot_and_api_start_from_unrelated_working_directory(self):
        project = self.root / "source"
        (project / "plugins").mkdir(parents=True)
        for rel in (
            "app_config.py", "bot.py", "binding_service.py", "database.py", "mc_api.py",
            "notify_client.py", "plugins/qq_bindings.py", "plugins/qq_events.py",
        ):
            shutil.copyfile(BOT_DIR / rel, project / rel)
        onebot_token = "76543210123456"
        (project / ".env").write_text(
            self.env_file.read_text(encoding="utf-8")
            + 'DRIVER=~websockets\nLOG_LEVEL=TRACE\nONEBOT_WS_URLS=["ws://127.0.0.1:13001/"]\n',
            encoding="utf-8",
        )
        decoy = self.root / "elsewhere"
        decoy.mkdir()
        (decoy / ".env").write_text("TARGET_GROUP_ID=invalid\nDRIVER=invalid\n", encoding="utf-8")
        code = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
before = Path.cwd()
import bot
import mc_api
import nonebot
from nonebot.adapters.onebot.v11 import Adapter
assert Path.cwd() == before
assert nonebot.get_driver().config.target_group_id == 98765
assert mc_api.TARGET_GROUP_ID == "98765"
adapter = nonebot.get_adapter(Adapter)
assert adapter.onebot_config.onebot_access_token == os.environ["ONEBOT_ACCESS_TOKEN"]
assert {plugin.module_name for plugin in nonebot.get_loaded_plugins()} == {
    "plugins.qq_bindings", "plugins.qq_events",
}
"""
        env = {
            key: value for key, value in os.environ.items()
            if key.upper() not in {
                *ENV, "DRIVER", "LOG_LEVEL", "ENVIRONMENT", "ONEBOT_ACCESS_TOKEN",
                "ONEBOT_V11_ACCESS_TOKEN", "ONEBOT_WS_URLS", "ONEBOT_V11_WS_URLS",
            }
        }
        env.update(TARGET_GROUP_ID="98765", ONEBOT_ACCESS_TOKEN=onebot_token, PYTHONUTF8="1")
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(project)],
            cwd=decoy, env=env, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        for token in (API_TOKEN, NOTIFY_TOKEN, onebot_token, "test-only.file-onebot"):
            if token in result.stdout or token in result.stderr:
                self.fail("Bot startup exposed a synthetic credential")
        self.assertEqual(result.returncode, 0, result.stderr)
