import asyncio
import json
import time
import unicodedata
from base64 import b64encode
from collections import OrderedDict
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from uuid import uuid4

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.permission import Permission

from app_config import ConfigError, load_config
from qq_group import group_rule
from runtime_status import HEALTH_PORT
from sudo_config import (
    MAX_RESULT_MESSAGE_BYTES,
    SUDO_SUBMIT_PATH,
    SUDO_TIMEOUT,
    SudoOutputSettings,
    is_single_command,
    read_sudo_output_settings,
    read_sudo_settings,
)

try:
    config = load_config()
    settings = read_sudo_settings(config)
    output_settings = read_sudo_output_settings(config) if settings else SudoOutputSettings()
except ConfigError as exc:
    logger.error("/sudo 已关闭：{}", exc)
    settings = None

running = asyncio.Lock()
recent: OrderedDict[tuple[int, int, int], float] = OrderedDict()
REPLAY_WINDOW = 300
MAX_RECENT = 1024
UPLOAD_TIMEOUT = 20
REPLIES = {
    "ok": "命令已返回，服务端无输出。",
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


def submit(token: str, payload: dict) -> tuple[str, str, bool]:
    # Fixed loopback address. The request cannot follow redirects or use an environment proxy.
    try:
        with closing(HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=SUDO_TIMEOUT)) as connection:
            connection.request("POST", SUDO_SUBMIT_PATH, json.dumps(payload), {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            with connection.getresponse() as response:
                if response.status != 200:
                    return ("unavailable" if response.status in {401, 403, 404} else "unknown"), "", False
                raw = response.read(MAX_RESULT_MESSAGE_BYTES + 1)
                result = json.loads(raw) if len(raw) <= MAX_RESULT_MESSAGE_BYTES else None
    except TimeoutError:
        return "timeout", "", False
    except (OSError, HTTPException, ValueError, RecursionError):
        return "unknown", "", False
    if isinstance(result, dict):
        status, output = result.get("status"), result.get("output", "")
        truncated = result.get("truncated", False)
        if (
            isinstance(status, str) and status in REPLIES and isinstance(output, str)
            and type(truncated) is bool
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in output)
        ):
            return status, output, truncated
    return "unknown", "", False


async def upload_output(bot: Bot, group_id: int, request_id: str, output: str) -> bool:
    try:
        async with asyncio.timeout(UPLOAD_TIMEOUT):
            result = await bot.call_api(
                "upload_group_file",
                group_id=str(group_id),
                name=f"sudo-{request_id}.txt",
                file="base64://" + b64encode(output.encode("utf-8")).decode("ascii"),
            )
    except Exception as exc:
        logger.warning("/sudo #{} 群文件上传失败（{}）", request_id, type(exc).__name__)
        return False
    if isinstance(result, dict) and isinstance(result.get("file_id"), str) and result["file_id"]:
        return True
    logger.warning("/sudo #{} 群文件上传未返回文件 ID", request_id)
    return False


@sudo.handle()
async def handle_sudo(bot: Bot, event: GroupMessageEvent) -> None:
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
                status, output, truncated = await asyncio.to_thread(submit, settings.bot_token, payload)
                if status == "unknown":
                    # A broken HTTP response cannot tell us whether the plugin executed.
                    await asyncio.sleep(max(0, wait.when() - asyncio.get_running_loop().time()))
                    status, output, truncated = "timeout", "", False
        except TimeoutError:
            status, output, truncated = "timeout", "", False
        logger.info("/sudo #{} qq={} result={}", request_id, event.user_id, status)
        if truncated or output_settings.use_file(output):
            if await upload_output(bot, event.group_id, request_id, output):
                return
            await sudo.finish(MessageSegment.text(f"结果文件上传失败或未确认。请求编号：{request_id}"))
            return
        text = "".join(
            char for char in output if char in "\r\n\t" or unicodedata.category(char) not in {"Cc", "Cs"}
        )
        if not text.strip():
            text = REPLIES[status]
        await sudo.finish(MessageSegment.text(text))
