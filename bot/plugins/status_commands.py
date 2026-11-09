import asyncio
import time
from weakref import WeakKeyDictionary

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.permission import SUPERUSER

import message_config
from qq_group import TARGET_GROUP_ID, group_rule
from runtime_status import check_fastapi, collect_status
from status_image import render_status


driver = get_driver()
connected_at: WeakKeyDictionary[Bot, float] = WeakKeyDictionary()
info_lock = asyncio.Lock()


@driver.on_bot_connect
async def remember_connection(bot: Bot) -> None:
    connected_at[bot] = time.monotonic()


@driver.on_bot_disconnect
async def forget_connection(bot: Bot) -> None:
    connected_at.pop(bot, None)


help_command = on_command("帮助", aliases={"help"}, rule=group_rule, priority=10, block=True)
ping = on_command("认证测试", aliases={"ping"}, rule=group_rule, priority=10, block=True)
info = on_command("信息", aliases={"info"}, rule=group_rule, permission=SUPERUSER, priority=10, block=True)


@help_command.handle()
async def handle_help() -> None:
    await help_command.finish(message_config.MESSAGES.render("commands.help.message"))


@ping.handle()
async def handle_ping() -> None:
    result = await asyncio.to_thread(check_fastapi)
    if result.status_code is not None:
        outcome = "success"
    else:
        outcome = {"Timeout": "timeout", "Connection failed": "connection_error"}.get(result.error, "error")
    await ping.finish(message_config.MESSAGES.render(
        f"commands.ping.{outcome}", status_code=result.status_code, latency_ms=round(result.latency_ms),
    ))


def _status_png(connected: bool, connected_for: float | None) -> bytes:
    return render_status(collect_status(str(TARGET_GROUP_ID), connected=connected, connected_for=connected_for))


@info.handle()
async def handle_info(bot: Bot) -> None:
    if info_lock.locked():
        await info.finish(message_config.MESSAGES.render("commands.info.busy"))
    async with info_lock:
        connected = driver.bots.get(bot.self_id) is bot
        started = connected_at.get(bot)
        connected_for = time.monotonic() - started if connected and started is not None else None
        try:
            png = await asyncio.to_thread(_status_png, connected, connected_for)
        except Exception as exc:
            logger.warning("[MC-QQ-AUTH] 状态图生成失败（{}）", type(exc).__name__)
            await info.finish(message_config.MESSAGES.render("commands.info.error"))
    await info.finish(MessageSegment.image(png, cache=False))
