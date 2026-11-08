import asyncio
import sqlite3

from nonebot import get_driver, logger, on_command
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule

from binding_service import (
    bind_player,
    get_binding,
    unbind_player,
)


TARGET_GROUP_ID = int(get_driver().config.target_group_id)


def is_target_group(event: Event) -> bool:
    """仅允许目标 QQ 群触发绑定指令。"""
    return (
        isinstance(event, GroupMessageEvent)
        and event.group_id == TARGET_GROUP_ID
    )


group_rule = Rule(is_target_group)


# ========================================
# /绑定 玩家名
# ========================================

bind = on_command(
    "绑定",
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
        await bind.finish("用法：/绑定 Minecraft玩家名")

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
        await bind.finish("绑定服务发生错误，请稍后重试")

    if status == "invalid_name":
        await bind.finish(
            "游戏 ID 格式不正确。\n"
            "仅支持 3～16 位英文字母、数字和下划线。"
        )

    if status == "already_bound":
        await bind.finish(
            f"你已经绑定了游戏 ID：{value}\n"
            "如需更换，请先使用 /解绑"
        )

    if status == "name_taken":
        await bind.finish(
            "该游戏 ID 已经被绑定，无法重复绑定。"
        )

    if status == "success":
        await bind.finish(
            f"绑定成功！\n"
            f"Minecraft ID：{value}\n"
            "你的绑定记录已保存。"
        )


# ========================================
# /我的绑定
# ========================================

query = on_command(
    "我的绑定",
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
        await query.finish("查询失败，请稍后重试")

    if mc_name is None:
        await query.finish(
            "你还没有绑定 Minecraft 游戏 ID。\n"
            "请使用 /绑定 玩家名"
        )

    await query.finish(
        f"当前绑定的 Minecraft ID：{mc_name}"
    )


# ========================================
# /解绑
# ========================================

unbind = on_command(
    "解绑",
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
        await unbind.finish("解绑失败，请稍后重试")

    if mc_name is None:
        await unbind.finish("你当前没有有效的绑定记录。")

    await unbind.finish(
        f"解绑成功！\n"
        f"原 Minecraft ID：{mc_name}\n"
        "绑定记录已删除。"
    )
