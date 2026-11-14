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
from sudo_config import SUDO_SUBMIT_PATH, SUDO_WS_PATH, SudoSettings, is_single_command


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
    async def connect(self, token="plugin-token"):
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})

        async def send(message):
            outgoing.put_nowait(message)

        task = asyncio.create_task(self.app({
            "type": "websocket", "scheme": "ws", "path": SUDO_WS_PATH,
            "headers": [(b"authorization", f"Bearer {token}".encode())], "query_string": b"",
        }, incoming.get, send))
        try:
            handshake = await asyncio.wait_for(outgoing.get(), 2)
            yield handshake["type"], incoming, outgoing
        finally:
            incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 2)

    @staticmethod
    def result(incoming, request_id, text="在线玩家 0/20"):
        incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({
            "id": request_id, "status": "ok", "output": text,
        })})

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
            self.result(incoming, "b" * 32)
            await asyncio.sleep(0)
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
