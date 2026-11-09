import asyncio
import time
from weakref import WeakKeyDictionary

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.permission import SUPERUSER

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
    await help_command.finish(
        "[MC-QQ-Auth] 指令帮助\n"
        "/绑定 游戏ID 或 /bind 游戏ID · 绑定 Minecraft 游戏 ID\n"
        "/我的绑定 或 /binding · 查询自己的绑定\n"
        "/解绑 或 /unbind · 撤销自己的绑定\n"
        "/认证测试 或 /ping · 检查授权 API\n"
        "/帮助 或 /help · 显示本帮助\n"
        "超级用户专用\n"
        "/信息 或 /info · 查看状态图片\n"
        "/授权对账 或 /reconcile · 核对群成员与授权"
    )


@ping.handle()
async def handle_ping() -> None:
    result = await asyncio.to_thread(check_fastapi)
    await ping.finish(f"[MC-QQ-Auth] Ping\nFastAPI Health: {result.summary}\nLatency: {result.latency_ms:.0f} ms")


def _status_png(connected: bool, connected_for: float | None) -> bytes:
    return render_status(collect_status(str(TARGET_GROUP_ID), connected=connected, connected_for=connected_for))


@info.handle()
async def handle_info(bot: Bot) -> None:
    if info_lock.locked():
        await info.finish("状态图正在生成，请稍后重试。")
    async with info_lock:
        connected = driver.bots.get(bot.self_id) is bot
        started = connected_at.get(bot)
        connected_for = time.monotonic() - started if connected and started is not None else None
        try:
            png = await asyncio.to_thread(_status_png, connected, connected_for)
        except Exception as exc:
            logger.warning("[MC-QQ-AUTH] 状态图生成失败（{}）", type(exc).__name__)
            await info.finish("状态图暂不可用，请稍后重试。")
    await info.finish(MessageSegment.image(png, cache=False))
