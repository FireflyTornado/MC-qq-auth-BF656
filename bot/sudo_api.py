import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from sudo_config import (
    MAX_RESULT_LENGTH,
    SUDO_SUBMIT_PATH,
    SUDO_TIMEOUT,
    SUDO_WS_PATH,
    SudoSettings,
    is_single_command,
)

logger = logging.getLogger("uvicorn.error.mc_qq_auth.sudo")
PLUGIN_RESULTS = {"ok", "rejected", "error", "busy", "expired"}
MAX_MESSAGE_BYTES = 8192
SEND_TIMEOUT = 3


@dataclass
class PendingCommand:
    request_id: str
    plugin: WebSocket
    result: asyncio.Future


def sudo_router(
    settings: SudoSettings | None,
    target_group: str,
    verify_bearer: Callable[[list[str] | None, bytes], bool],
) -> APIRouter:
    router = APIRouter()
    plugin: WebSocket | None = None
    ready = False
    pending: PendingCommand | None = None

    @router.post(SUDO_SUBMIT_PATH)
    async def submit(request: Request, authorization: list[str] | None = Header(default=None)):
        nonlocal pending
        if settings is None:
            raise HTTPException(404)
        if not verify_bearer(authorization, settings.bot_token.encode("ascii")):
            raise HTTPException(401)

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_MESSAGE_BYTES:
                raise HTTPException(413)
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(400) from None
        if not isinstance(data, dict):
            raise HTTPException(400)
        qq_id = data.get("qq_id")
        if data.get("group_id") != target_group or not isinstance(qq_id, str) or qq_id not in settings.qq_ids:
            raise HTTPException(403)
        command = data.get("command")
        request_id = data.get("id")
        deadline_ms = data.get("deadline_ms")
        if (
            not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
            or not isinstance(command, str) or not is_single_command(command)
            or type(deadline_ms) is not int
        ):
            raise HTTPException(400)
        remaining = deadline_ms - time.time_ns() // 1_000_000
        if remaining <= 0:
            return {"status": "expired"}
        if remaining > SUDO_TIMEOUT * 1000:
            raise HTTPException(400)
        if pending is not None:
            return {"status": "busy"}
        if plugin is None or not ready:
            return {"status": "unavailable"}

        work = PendingCommand(request_id, plugin, asyncio.get_running_loop().create_future())
        pending = work
        logger.info("/sudo #%s group=%s qq=%s command=%r", request_id, target_group, qq_id, command)
        status = "timeout"
        try:
            async with asyncio.timeout(remaining / 1000):
                try:
                    async with asyncio.timeout(SEND_TIMEOUT):
                        await work.plugin.send_json({
                            "id": request_id,
                            "command": command,
                            "deadline_ms": deadline_ms,
                        })
                except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
                    # An interrupted write can still have reached the game server.
                    logger.warning("/sudo #%s 未能确认指令已发送", request_id)
                result = await work.result
                status = result["status"]
                return result
        except TimeoutError:
            return {"status": status}
        finally:
            pending = None
            if not work.result.done():
                work.result.cancel()
            logger.info("/sudo #%s result=%s", request_id, status)

    @router.websocket(SUDO_WS_PATH)
    async def results(websocket: WebSocket):
        nonlocal plugin, ready
        if (
            settings is None or plugin is not None
            or not verify_bearer(websocket.headers.getlist("authorization"), settings.plugin_token.encode("ascii"))
        ):
            await websocket.close(code=1008)
            return

        plugin = websocket
        try:
            await websocket.accept()
            ready = True
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    await websocket.close(code=1003 if raw is None else 1009)
                    break
                try:
                    result = json.loads(raw)
                except (ValueError, RecursionError):
                    await websocket.close(code=1007)
                    break
                if not isinstance(result, dict):
                    await websocket.close(code=1007)
                    break
                status, output = result.get("status"), result.get("output")
                if (
                    not isinstance(status, str) or status not in PLUGIN_RESULTS
                    or not isinstance(output, str) or len(output) > MAX_RESULT_LENGTH
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in output)
                ):
                    await websocket.close(code=1007)
                    break
                if (
                    pending is not None and pending.plugin is websocket and not pending.result.done()
                    and result.get("id") == pending.request_id
                ):
                    pending.result.set_result({"status": status, "output": output})
        finally:
            ready = False
            plugin = None

    return router
