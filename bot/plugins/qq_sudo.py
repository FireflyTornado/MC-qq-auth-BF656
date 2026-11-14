import asyncio
import json
import time
import unicodedata
from base64 import b64encode
from collections import OrderedDict
from uuid import uuid4

import h11
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
replay_cache_blocked_until = 0.0
UPLOAD_TIMEOUT = 20
REPLIES = {
    "ok": "命令已返回，服务端无输出。",
    "rejected": "该指令或参数未获服务端许可。",
    "error": "服务端执行失败或无法确认结果。",
    "busy": "有指令正在处理，请稍后重试。",
    "unavailable": "Minecraft 指令通道当前离线。",
    "disconnected": "指令通道已断开，执行结果未知。",
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


async def submit(token: str, payload: dict) -> tuple[str, str, bool]:
    # Socket I/O is cancellable under the handler's total deadline.
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", HEALTH_PORT)
        try:
            protocol = h11.Connection(h11.CLIENT)
            body = json.dumps(payload).encode("utf-8")
            request = h11.Request(method="POST", target=SUDO_SUBMIT_PATH, headers=[
                ("Host", f"127.0.0.1:{HEALTH_PORT}"),
                ("Authorization", f"Bearer {token}"),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ])
            writer.write(protocol.send(request))
            writer.write(protocol.send(h11.Data(data=body)))
            writer.write(protocol.send(h11.EndOfMessage()))
            await writer.drain()

            raw = bytearray()
            while True:
                event = protocol.next_event()
                if event is h11.NEED_DATA:
                    protocol.receive_data(await reader.read(8192))
                elif isinstance(event, h11.Response) and event.status_code != 200:
                    return ("unavailable" if event.status_code in {401, 403, 404} else "unknown"), "", False
                elif isinstance(event, h11.Data):
                    if len(raw) + len(event.data) > MAX_RESULT_MESSAGE_BYTES:
                        return "unknown", "", False
                    raw.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
                elif isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                    return "unknown", "", False
            result = json.loads(raw)
        finally:
            # Abort immediately, including if cancellation interrupted a write or read.
            writer.transport.abort()
    except (OSError, h11.ProtocolError, ValueError, RecursionError):
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
    global replay_cache_blocked_until

    if settings is None:
        return
    command = command_from(event)
    if command is None:
        await sudo.finish(MessageSegment.text("用法：/sudo <一条纯文本 Minecraft 指令>"))
        return
    now = time.monotonic()
    while recent and next(iter(recent.values())) <= now - REPLAY_WINDOW:
        recent.popitem(last=False)
    key = (event.self_id, event.group_id, event.message_id)
    if key in recent:
        return
    if len(recent) >= MAX_RECENT or now < replay_cache_blocked_until:
        # Reject untracked messages until their redelivery window has passed.
        replay_cache_blocked_until = now + REPLAY_WINDOW
        await sudo.finish(MessageSegment.text("指令请求过多，暂无法受理。请等待至少 5 分钟后重新发送。"))
        return
    recent[key] = now

    if running.locked():
        await sudo.finish(MessageSegment.text(REPLIES["busy"]))
        return
    async with running:
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
                status, output, truncated = await submit(settings.bot_token, payload)
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
