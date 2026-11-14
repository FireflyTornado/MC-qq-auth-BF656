import asyncio
import json
import time
import unicodedata
from collections import OrderedDict
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from uuid import uuid4

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.permission import Permission

from app_config import ConfigError, load_config
from qq_group import group_rule
from runtime_status import HEALTH_PORT
from sudo_config import SUDO_SUBMIT_PATH, SUDO_TIMEOUT, is_single_command, read_sudo_settings

try:
    settings = read_sudo_settings(load_config())
except ConfigError as exc:
    logger.error("/sudo 已关闭：{}", exc)
    settings = None

running = asyncio.Lock()
recent: OrderedDict[tuple[int, int, int], float] = OrderedDict()
REPLAY_WINDOW = 300
MAX_RECENT = 1024
MAX_HTTP_RESPONSE = 8192
REPLIES = {
    "ok": "指令调用已完成。",
    "rejected": "该指令或参数未获服务端许可。",
    "error": "服务端执行失败或无法确认结果。",
    "busy": "有指令正在处理，请稍后重试。",
    "unavailable": "Minecraft 指令通道当前离线。",
    "expired": "指令已过期，未执行。",
    "timeout": "执行超时，结果未知。",
}


def allowed(event: GroupMessageEvent) -> bool:
    return (
        settings is not None
        and event.sub_type == "normal"
        and event.anonymous is None
        and event.user_id != event.self_id
        and str(event.user_id) in settings.qq_ids
    )


sudo = on_command("sudo", rule=group_rule, permission=Permission(allowed), force_whitespace=True,
                  priority=10, block=True)


def command_from(event: GroupMessageEvent) -> str | None:
    message = event.original_message
    if not message or not all(segment.is_text() for segment in message):
        return None
    prefix, _, command = message.extract_plain_text().partition(" ")
    if prefix != "/sudo":
        return None
    command = command.strip(" ")
    return command if is_single_command(command) else None


def submit(token: str, payload: dict) -> tuple[str, str]:
    # Fixed loopback address. The request cannot follow redirects or use an environment proxy.
    try:
        with closing(HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=SUDO_TIMEOUT)) as connection:
            connection.request("POST", SUDO_SUBMIT_PATH, json.dumps(payload), {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            with connection.getresponse() as response:
                if response.status != 200:
                    return ("unavailable" if response.status in {401, 403, 404} else "unknown"), ""
                raw = response.read(MAX_HTTP_RESPONSE + 1)
                result = json.loads(raw) if len(raw) <= MAX_HTTP_RESPONSE else None
    except TimeoutError:
        return "timeout", ""
    except (OSError, HTTPException, ValueError, RecursionError):
        return "unknown", ""
    if isinstance(result, dict):
        status, output = result.get("status"), result.get("output", "")
        if isinstance(status, str) and status in REPLIES and isinstance(output, str):
            return status, "".join(
                char for char in output if char == "\n" or unicodedata.category(char)[0] != "C"
            )
    return "unknown", ""


@sudo.handle()
async def handle_sudo(event: GroupMessageEvent) -> None:
    if settings is None:
        return
    command = command_from(event)
    if command is None:
        await sudo.finish(MessageSegment.text("用法：/sudo <一条纯文本 Minecraft 指令>"))
        return
    if running.locked():
        await sudo.finish(MessageSegment.text(REPLIES["busy"]))
        return
    async with running:
        now = time.monotonic()
        while recent and next(iter(recent.values())) <= now - REPLAY_WINDOW:
            recent.popitem(last=False)
        key = (event.self_id, event.group_id, event.message_id)
        if key in recent:
            return
        recent[key] = now
        if len(recent) > MAX_RECENT:
            recent.popitem(last=False)
        request_id = uuid4().hex
        payload = {
            "id": request_id,
            "group_id": str(event.group_id),
            "qq_id": str(event.user_id),
            "command": command,
            "deadline_ms": time.time_ns() // 1_000_000 + SUDO_TIMEOUT * 1000,
        }
        try:
            async with asyncio.timeout(SUDO_TIMEOUT) as wait:
                status, output = await asyncio.to_thread(submit, settings.bot_token, payload)
                if status == "unknown":
                    # A broken HTTP response cannot tell us whether the plugin executed.
                    await asyncio.sleep(max(0, wait.when() - asyncio.get_running_loop().time()))
                    status, output = "timeout", ""
        except TimeoutError:
            status, output = "timeout", ""
        logger.info("/sudo #{} qq={} result={}", request_id, event.user_id, status)
        text = REPLIES[status]
        if output:
            text += "\n" + output
        await sudo.finish(MessageSegment.text(text))
