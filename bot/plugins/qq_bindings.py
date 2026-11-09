import asyncio
import sqlite3

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
)
from nonebot.params import CommandArg

from binding_service import (
    bind_player,
    get_binding,
    unbind_player,
)
import message_config
from qq_group import group_rule


# ========================================
# /绑定 玩家名
# ========================================

bind = on_command(
    "绑定",
    aliases={"bind"},
    rule=group_rule,
    priority=10,
    block=True,
)


@bind.handle()
async def handle_bind(
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    mc_name = args.extract_plain_text().strip()

    if not mc_name:
        await bind.finish(message_config.MESSAGES.render("commands.bind.usage"))

    group_id = str(event.group_id)
    qq_id = str(event.user_id)

    try:
        status, value = await asyncio.to_thread(
            bind_player,
            group_id,
            qq_id,
            mc_name,
        )
    except sqlite3.Error:
        logger.exception("绑定数据库操作失败")
        await bind.finish(message_config.MESSAGES.render("commands.bind.error"))

    await bind.finish(message_config.MESSAGES.render(f"commands.bind.{status}", mc_name=value))


# ========================================
# /我的绑定
# ========================================

query = on_command(
    "我的绑定",
    aliases={"binding"},
    rule=group_rule,
    priority=10,
    block=True,
)


@query.handle()
async def handle_query(event: GroupMessageEvent):
    try:
        mc_name = await asyncio.to_thread(
            get_binding,
            str(event.group_id),
            str(event.user_id),
        )
    except sqlite3.Error:
        logger.exception("查询绑定数据库失败")
        await query.finish(message_config.MESSAGES.render("commands.binding.error"))

    if mc_name is None:
        await query.finish(message_config.MESSAGES.render("commands.binding.not_bound"))

    await query.finish(message_config.MESSAGES.render("commands.binding.success", mc_name=mc_name))


# ========================================
# /解绑
# ========================================

unbind = on_command(
    "解绑",
    aliases={"unbind"},
    rule=group_rule,
    priority=10,
    block=True,
)


@unbind.handle()
async def handle_unbind(event: GroupMessageEvent):
    try:
        mc_name = await asyncio.to_thread(
            unbind_player,
            str(event.group_id),
            str(event.user_id),
        )
    except sqlite3.Error:
        logger.exception("解绑数据库操作失败")
        await unbind.finish(message_config.MESSAGES.render("commands.unbind.error"))

    if mc_name is None:
        await unbind.finish(message_config.MESSAGES.render("commands.unbind.not_bound"))

    await unbind.finish(message_config.MESSAGES.render("commands.unbind.success", mc_name=mc_name))
