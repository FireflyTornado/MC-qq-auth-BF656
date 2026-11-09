import asyncio
import sqlite3
from collections import OrderedDict

from nonebot import get_driver, logger, on_command, on_type
from nonebot.adapters import Event

from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
)
from nonebot.adapters.onebot.v11.exception import OneBotV11AdapterException
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule

from binding_service import binding_audit_snapshot, revoke_checked_bindings, unbind_player

driver = get_driver()
TARGET_GROUP_ID = int(driver.config.target_group_id)
GROUP_ID = str(TARGET_GROUP_ID)
MEMBER_CHECK_TIMEOUT = 20
MEMBER_CHECK_RETRY_DELAY = 5
RECENT_DEPARTURE_LIMIT = 1024
reconcile_lock = asyncio.Lock()
membership_generation = 0
recent_departures: OrderedDict[tuple[int, int, int, str], None] = OrderedDict()
bots_outside_group: set[str] = set()


def is_target_group(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and event.group_id == TARGET_GROUP_ID


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
    global membership_generation
    if event.group_id != TARGET_GROUP_ID:
        return

    membership_generation += 1
    if event.user_id == event.self_id:
        bots_outside_group.discard(str(event.self_id))
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
    global membership_generation
    if event.group_id != TARGET_GROUP_ID:
        return

    if event.sub_type == "kick_me" or event.user_id == event.self_id:
        membership_generation += 1
        bots_outside_group.add(str(event.self_id))
        logger.error("[MC-QQ-AUTH] 机器人已离开目标群，群成员授权对账不可用")
        return

    if event.sub_type not in {"leave", "kick"}:
        return

    key = (event.user_id, event.time, event.operator_id, event.sub_type)
    if key in recent_departures:
        return
    recent_departures[key] = None
    if len(recent_departures) > RECENT_DEPARTURE_LIMIT:
        recent_departures.popitem(last=False)
    membership_generation += 1
    reason = "成员主动退群" if event.sub_type == "leave" else "成员被管理员移出群"
    try:
        name = await asyncio.to_thread(
            unbind_player, GROUP_ID, str(event.user_id), left_at=event.time,
        )
    except sqlite3.Error as exc:
        recent_departures.pop(key, None)
        logger.error("[MC-QQ-AUTH] {}后的撤权事务失败（{}）", reason, type(exc).__name__)
        return
    if name is not None:
        logger.info("[MC-QQ-AUTH] {}，已撤销绑定", reason)


class UnverifiedMembers(ValueError):
    pass


def member_ids(rows: object, self_id: str) -> set[str]:
    if not isinstance(rows, list) or not rows:
        raise UnverifiedMembers

    members = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("group_id")) is not int or row["group_id"] != TARGET_GROUP_ID:
            raise UnverifiedMembers
        user_id = row.get("user_id")
        if type(user_id) is not int or user_id <= 0 or str(user_id) in members:
            raise UnverifiedMembers
        members.add(str(user_id))
    if self_id not in members:
        raise UnverifiedMembers
    return members


async def reconcile_members(bot: Bot) -> int | None:
    async with reconcile_lock:
        if driver.bots.get(bot.self_id) is not bot or bot.self_id in bots_outside_group:
            return None
        generation = membership_generation
        try:
            version, bindings = await asyncio.to_thread(binding_audit_snapshot, GROUP_ID)
            if not bindings:
                return 0

            # NapCat may serve a cached list while refreshing it. Verify two reads
            # against the independent group details before treating absence as evidence.
            async with asyncio.timeout(MEMBER_CHECK_TIMEOUT):
                first = member_ids(
                    await bot.get_group_member_list(group_id=TARGET_GROUP_ID, no_cache=True), bot.self_id,
                )
                detail = await bot.call_api("get_group_detail_info", group_id=TARGET_GROUP_ID)
                second = member_ids(
                    await bot.get_group_member_list(group_id=TARGET_GROUP_ID, no_cache=True), bot.self_id,
                )
            count = detail.get("member_count") if isinstance(detail, dict) else None
            if (type(count) is not int or count != len(second) or first != second
                    or type(detail.get("group_id")) is not int or detail["group_id"] != TARGET_GROUP_ID
                    or membership_generation != generation or driver.bots.get(bot.self_id) is not bot):
                raise UnverifiedMembers

            missing = {qq_id: bindings[qq_id] for qq_id in bindings.keys() - second}
            deleted = await asyncio.to_thread(revoke_checked_bindings, GROUP_ID, version, missing)
            if deleted is None:
                logger.warning("[MC-QQ-AUTH] 对账期间授权版本已变化，本次对账已跳过")
            return deleted
        except (UnverifiedMembers, TimeoutError, OneBotV11AdapterException) as exc:
            logger.warning("[MC-QQ-AUTH] 群成员名单无法确认，对账已跳过（{}）", type(exc).__name__)
        except sqlite3.Error as exc:
            logger.error("[MC-QQ-AUTH] 授权对账数据库操作失败（{}）", type(exc).__name__)
        return None


@driver.on_bot_connect
async def reconcile_on_connect(bot: Bot):
    if not isinstance(bot, Bot):
        return
    count = await reconcile_members(bot)
    if count is None and driver.bots.get(bot.self_id) is bot and bot.self_id not in bots_outside_group:
        await asyncio.sleep(MEMBER_CHECK_RETRY_DELAY)
        count = await reconcile_members(bot)
    if count is not None:
        logger.info("[MC-QQ-AUTH] OneBot 连接后授权对账完成，已撤销 {} 条绑定", count)


reconcile = on_command("授权对账", rule=Rule(is_target_group), permission=SUPERUSER, priority=10, block=True)


@reconcile.handle()
async def handle_reconcile(bot: Bot, event: GroupMessageEvent):
    if reconcile_lock.locked():
        await reconcile.finish("授权对账正在执行，请稍后重试。")
    count = await reconcile_members(bot)
    if count is None:
        await reconcile.finish("授权对账未完成，请检查群成员名单和服务日志。")
    await reconcile.finish(f"授权对账完成，已撤销 {count} 条离群绑定。")
