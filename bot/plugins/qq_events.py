from nonebot import get_driver, logger, on_command, on_type

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
)


TARGET_GROUP_ID = int(
    get_driver().config.target_group_id
)


# 1. 测试群消息
ping = on_command(
    "认证测试",
    priority=10,
    block=True,
)


@ping.handle()
async def handle_ping(event: GroupMessageEvent):
    if event.group_id != TARGET_GROUP_ID:
        return

    await ping.finish(
        "Minecraft 社区认证机器人运行正常！"
    )


# 2. 监听成员入群
member_join = on_type(
    GroupIncreaseNoticeEvent,
    priority=10,
    block=False,
)


@member_join.handle()
async def handle_member_join(
    event: GroupIncreaseNoticeEvent,
):
    if event.group_id != TARGET_GROUP_ID:
        return

    logger.info(
        "[MC-QQ-AUTH] 检测到目标群成员加入事件"
    )


# 3. 监听成员退群
member_leave = on_type(
    GroupDecreaseNoticeEvent,
    priority=10,
    block=False,
)


@member_leave.handle()
async def handle_member_leave(
    event: GroupDecreaseNoticeEvent,
):
    if event.group_id != TARGET_GROUP_ID:
        return

    if event.sub_type == "leave":
        logger.info(
            "[MC-QQ-AUTH] 检测到成员主动退群"
        )

    elif event.sub_type == "kick":
        logger.info(
            "[MC-QQ-AUTH] 检测到成员被移出群"
        )

    elif event.sub_type == "kick_me":
        logger.warning(
            "[MC-QQ-AUTH] 机器人被移出目标群"
        )
