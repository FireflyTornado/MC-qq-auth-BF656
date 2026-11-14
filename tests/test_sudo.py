import asyncio
import json
import sys
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from sudo_api import sudo_router
from app_config import ConfigError
from sudo_config import MAX_RESULT_BYTES, SUDO_SUBMIT_PATH, SUDO_WS_PATH, SudoSettings, is_single_command, read_sudo_output_settings


class SudoOutputSettingsTests(unittest.TestCase):
    def test_threshold_boundaries_and_config(self):
        defaults = read_sudo_output_settings({})
        self.assertFalse(defaults.use_file("a\r\n" * 10))
        self.assertTrue(defaults.use_file("a\r\n" * 11))
        self.assertFalse(defaults.use_file("字" * 1500))
        self.assertTrue(defaults.use_file("字" * 1501))
        custom = read_sudo_output_settings({
            "MC_SUDO_FILE_LINE_THRESHOLD": "2",
            "MC_SUDO_FILE_LINE_CHAR_THRESHOLD": "3",
            "MC_SUDO_TEXT_MAX_CHARS": "8",
        })
        self.assertFalse(custom.use_file("甲乙丙\n丁\n"))
        self.assertTrue(custom.use_file("甲乙丙丁\n"))
        self.assertTrue(custom.use_file("甲\n乙\n丙\n"))
        self.assertTrue(custom.use_file("aaa\r\nbbb\n"))
        with self.assertRaises(ConfigError):
            read_sudo_output_settings({"MC_SUDO_TEXT_MAX_CHARS": "0"})


class SudoApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(sudo_router(
            SudoSettings(frozenset({"101"}), "bot-token", "plugin-token"), "202",
            lambda headers, token: headers == ["Bearer " + token.decode("ascii")],
        ))

    async def submit(self, *, token="bot-token", timeout=2, request_id="a" * 32, **overrides):
        payload = {
            "id": request_id,
            "group_id": "202",
            "qq_id": "101",
            "command": "list",
            "deadline_ms": time.time_ns() // 1_000_000 + round(timeout * 1000),
            **overrides,
        }
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode()}

        await self.app({
            "type": "http", "method": "POST", "scheme": "http", "path": SUDO_SUBMIT_PATH,
            "headers": [(b"authorization", f"Bearer {token}".encode())], "query_string": b"",
        }, receive, send)
        return sent[0]["status"], json.loads(sent[-1]["body"])

    @asynccontextmanager
    async def connect(self, token="plugin-token", *, block_send=None):
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})

        async def receive():
            message = await incoming.get()
            if isinstance(message, asyncio.Event):
                # The handler has returned to receive after processing the preceding frame.
                message.set()
                message = await incoming.get()
            return message

        async def send(message):
            outgoing.put_nowait(message)
            if block_send is not None and message["type"] == "websocket.send":
                await block_send.wait()

        task = asyncio.create_task(self.app({
            "type": "websocket", "scheme": "ws", "path": SUDO_WS_PATH,
            "headers": [(b"authorization", f"Bearer {token}".encode())], "query_string": b"",
        }, receive, send))
        try:
            handshake = await asyncio.wait_for(outgoing.get(), 2)
            yield handshake["type"], incoming, outgoing
        finally:
            incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 2)

    @staticmethod
    def result(incoming, request_id, text="在线玩家 0/20", **fields):
        incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({
            "id": request_id, "status": "ok", "output": text, **fields,
        })})

    async def test_forwards_full_byte_budget_and_truncation_flag(self):
        output = "\x00" * MAX_RESULT_BYTES  # Worst-case JSON expansion of a UTF-8 byte.
        async with self.connect() as (_, incoming, outgoing):
            first = asyncio.create_task(self.submit())
            request = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            self.result(incoming, request["id"], output, truncated=True)
            self.assertEqual(await first, (200, {"status": "ok", "output": output, "truncated": True}))

    async def test_permission_and_single_command_validation(self):
        self.assertEqual((await self.submit(qq_id="102"))[0], 403)
        self.assertEqual((await self.submit(group_id="303"))[0], 403)
        self.assertEqual((await self.submit(token="plugin-token"))[0], 401)
        self.assertEqual((await self.submit(command="list\nsay hello"))[0], 400)
        self.assertFalse(is_single_command("say first\u2028second"))
        self.assertEqual(await self.submit(), (200, {"status": "unavailable"}))
        async with self.connect("bot-token") as (handshake, _, _):
            self.assertEqual(handshake, "websocket.close")

    async def test_waits_for_matching_result_and_refuses_concurrent_work(self):
        async with self.connect() as (handshake, incoming, outgoing):
            self.assertEqual(handshake, "websocket.accept")
            async with self.connect() as (second_handshake, _, _):
                self.assertEqual(second_handshake, "websocket.close")
            first = asyncio.create_task(self.submit())
            request = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            self.assertEqual(request["command"], "list")
            self.assertEqual(await self.submit(request_id="b" * 32), (200, {"status": "busy"}))
            self.result(incoming, "b" * 32, "wrong request output")
            processed = asyncio.Event()
            incoming.put_nowait(processed)
            await asyncio.wait_for(processed.wait(), 2)
            self.assertFalse(first.done())
            self.result(incoming, request["id"])
            self.assertEqual(await first, (200, {"status": "ok", "output": "在线玩家 0/20"}))

    async def test_timeout_releases_slot_and_late_result_is_ignored(self):
        async with self.connect() as (_, incoming, outgoing):
            first = asyncio.create_task(self.submit(timeout=0.15))
            prior = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            self.assertEqual(await first, (200, {"status": "timeout"}))
            next_request = asyncio.create_task(self.submit(request_id="b" * 32))
            current = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            self.result(incoming, prior["id"], "old result")
            await asyncio.sleep(0)
            self.assertFalse(next_request.done())
            self.result(incoming, current["id"])
            self.assertEqual((await next_request)[1], {"status": "ok", "output": "在线玩家 0/20"})

    async def test_disconnect_releases_pending_command_even_if_send_is_blocked(self):
        async with self.connect(block_send=asyncio.Event()) as (_, _, outgoing):
            first = asyncio.create_task(self.submit(timeout=30))
            await asyncio.wait_for(outgoing.get(), 2)

        self.assertEqual(await asyncio.wait_for(first, 1), (200, {"status": "disconnected"}))
        self.assertEqual(await self.submit(), (200, {"status": "unavailable"}))
        async with self.connect() as (_, incoming, outgoing):
            next_request = asyncio.create_task(self.submit(request_id="b" * 32))
            request = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            self.result(incoming, request["id"], "after reconnect")
        # The matching result was queued before the disconnect and must win.
        self.assertEqual(await next_request, (200, {"status": "ok", "output": "after reconnect"}))
