import asyncio
import time
from unicodedata import category
from weakref import WeakKeyDictionary

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.permission import SUPERUSER

import message_config
from app_config import ConfigError, info_layout, load_config
from qq_avatar import get_avatar, is_qq_id
from qq_group import TARGET_GROUP_ID, group_rule
from runtime_status import check_fastapi, collect_status
from status_image import render_custom_status, render_status


driver = get_driver()
connected_at: WeakKeyDictionary[Bot, float] = WeakKeyDictionary()
info_lock = asyncio.Lock()
LOGIN_INFO_TIMEOUT = 2


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


async def _nickname(bot: Bot) -> str | None:
    try:
        async with asyncio.timeout(LOGIN_INFO_TIMEOUT):
            identity = await bot.get_login_info()
    except Exception as exc:
        logger.debug("[MC-QQ-AUTH] OneBot 登录信息不可用（{}）", type(exc).__name__)
        return None
    if not isinstance(identity, dict) or str(identity.get("user_id")) != bot.self_id:
        return None
    nickname = identity.get("nickname")
    if not isinstance(nickname, str):
        return None
    nickname = "".join(ch for ch in nickname[:256] if category(ch) not in {"Cc", "Cf", "Cs"})
    return nickname.strip() or None


def _status_png(qq_id: str, nickname: str | None, connected: bool, connected_for: float | None) -> bytes:
    try:
        layout = info_layout(load_config())
    except ConfigError as exc:
        logger.warning("[MC-QQ-AUTH] {}，状态图使用 default", exc)
        layout = "default"
    render = render_custom_status if layout == "custom" else render_status
    avatar = get_avatar(qq_id)
    status = collect_status(str(TARGET_GROUP_ID), connected=connected, connected_for=connected_for)
    return render(status, qq_id=qq_id, nickname=nickname, avatar=avatar)


@info.handle()
async def handle_info(bot: Bot) -> None:
    if info_lock.locked():
        await info.finish(message_config.MESSAGES.render("commands.info.busy"))
    async with info_lock:
        qq_id = bot.self_id if is_qq_id(bot.self_id) else ""
        nickname = await _nickname(bot) if qq_id and driver.bots.get(qq_id) is bot else None
        connected = driver.bots.get(bot.self_id) is bot
        started = connected_at.get(bot)
        connected_for = time.monotonic() - started if connected and started is not None else None
        try:
            png = await asyncio.to_thread(_status_png, qq_id, nickname, connected, connected_for)
        except Exception as exc:
            logger.warning("[MC-QQ-AUTH] 状态图生成失败（{}）", type(exc).__name__)
            await info.finish(message_config.MESSAGES.render("commands.info.error"))
    await info.finish(MessageSegment.image(png, cache=False))
