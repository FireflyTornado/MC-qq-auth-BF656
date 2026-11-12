import asyncio
import base64
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import chdir, closing
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import nonebot
from PIL import Image
from nonebot.adapters.onebot.v11 import (
    Adapter, Bot, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent, GroupMessageEvent, Message,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.rule import TrieRule

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import binding_service
import database
import message_config
import runtime_status

GROUP = 101
OTHER_GROUP = 102
BOT = 201
MEMBER = 202
ABSENT = 203
DEPARTED_AT = 1_800_000_000


def departure(sub_type="leave", group=GROUP, user=ABSENT, timestamp=DEPARTED_AT):
    return GroupDecreaseNoticeEvent(
        time=timestamp, self_id=BOT, post_type="notice", notice_type="group_decrease",
        sub_type=sub_type, group_id=group, user_id=user, operator_id=MEMBER,
    )


def arrival(group=GROUP, user=ABSENT, timestamp=DEPARTED_AT):
    return GroupIncreaseNoticeEvent(
        time=timestamp, self_id=BOT, post_type="notice", notice_type="group_increase",
        sub_type="approve", group_id=group, user_id=user, operator_id=MEMBER,
    )


def member_rows(*members):
    return [{"group_id": GROUP, "user_id": user} for user in members]


class QQRevocationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {}, clear=True):
            nonebot.init(_env_file=(), driver="~websockets", target_group_id=GROUP, superusers={str(MEMBER)})
        cls.driver = nonebot.get_driver()
        cls.driver.register_adapter(Adapter)
        cls.events = importlib.import_module("plugins.qq_events")
        importlib.import_module("plugins.qq_bindings")
        cls.status = importlib.import_module("plugins.status_commands")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "auth.db"
        database.init_database(self.path)
        self.enterContext(patch.object(binding_service, "DB_PATH", self.path))
        self.enterContext(patch.object(database, "DB_PATH", self.path))
        self.enterContext(patch.object(message_config, "MESSAGES", message_config.MessageConfig(message_config.DEFAULTS)))
        self.enterContext(patch.object(runtime_status, "_outcomes", {}))
        self.enterContext(patch.object(runtime_status, "check_minecraft", return_value=None))
        self.enterContext(patch.object(self.status, "get_avatar", return_value=None))
        notify_patch = patch.object(binding_service, "notify_auth_updated", return_value=True)
        self.notify = notify_patch.start()
        self.addCleanup(notify_patch.stop)
        self.events.recent_departures.clear()
        self.events.bots_outside_group.clear()
        self.bot = Bot(nonebot.get_adapter(Adapter), str(BOT))
        self.send_group = self.enterContext(patch.object(self.bot, "send_group_msg", new_callable=AsyncMock))
        self.reply = self.enterContext(patch.object(self.bot, "send", new_callable=AsyncMock))
        self.driver.bots[self.bot.self_id] = self.bot
        self.addCleanup(self.driver.bots.pop, self.bot.self_id, None)
        self.addCleanup(self.events.recent_departures.clear)
        self.addCleanup(self.events.bots_outside_group.clear)

    def bind(self, user=ABSENT, name="Steve", group=GROUP, timestamp=DEPARTED_AT - 1):
        with patch.object(binding_service.time, "time", return_value=timestamp):
            self.assertEqual(binding_service.bind_player(str(group), str(user), name), ("success", name))

    def binding(self, user=ABSENT, group=GROUP):
        return binding_service.get_binding(str(group), str(user))

    def configure(self, source):
        path = self.path.with_name("messages.yml")
        path.write_text(source, encoding="utf-8")
        return patch.object(message_config, "MESSAGES", message_config.load_messages(path))

    def assert_notice(self, expected):
        self.send_group.assert_awaited_once_with(group_id=GROUP, message=Message(expected))

    def enable_notices(self, *events):
        self.enterContext(self.configure("events:\n" + "".join(f"  {event}: {{enabled: true}}\n" for event in events)))

    def version(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT version FROM auth_state").fetchone()[0]

    def mock_roster(self, first, second=None, *, count=2, detail_group=GROUP):
        responses = iter((first, first if second is None else second))

        async def api(action, **data):
            if action == "get_group_detail_info":
                self.assertEqual(data, {"group_id": GROUP})
                return {"group_id": detail_group, "member_count": count}
            self.assertEqual(action, "get_group_member_list")
            self.assertEqual(data, {"group_id": GROUP, "no_cache": True})
            return next(responses)

        return patch.object(self.bot, "call_api", AsyncMock(side_effect=api))

    async def test_leave_revokes_only_target_group_and_notifies_after_commit(self):
        self.enable_notices("leave", "join")
        self.bind()
        self.bind(name="Other", group=OTHER_GROUP)
        old_version = self.version()
        self.notify.reset_mock()

        def verify_commit():
            self.assertIsNone(self.binding())
            self.assertGreater(self.version(), old_version)
            return True

        self.notify.side_effect = verify_commit
        await self.events.handle_member_leave(self.bot, departure())
        self.assertEqual(self.binding(group=OTHER_GROUP), "Other")
        self.notify.assert_called_once_with()
        self.assert_notice("成员已退出群聊。Minecraft 授权已自动撤销。")
        await self.events.handle_member_join(self.bot, arrival(timestamp=DEPARTED_AT + 1))
        self.assertIsNone(self.binding())
        welcome = self.send_group.call_args.kwargs["message"]
        self.assertEqual(welcome[0].type, "at")
        self.assertEqual(welcome[0].data["qq"], str(ABSENT))
        for command in ("/绑定 游戏ID", "/bind 游戏ID", "/帮助", "/help"):
            self.assertIn(command, welcome.extract_plain_text())

    async def test_kick_revokes_the_member_not_the_operator(self):
        self.enable_notices("kick")
        self.bind()
        self.bind(MEMBER, "Alex")
        self.notify.reset_mock()
        await self.events.handle_member_leave(self.bot, departure("kick"))
        self.assertIsNone(self.binding())
        self.assertEqual(self.binding(MEMBER), "Alex")
        self.notify.assert_called_once_with()
        self.assert_notice("成员已被移出群聊。Minecraft 授权已自动撤销。")

    async def test_other_group_and_unbound_departures_are_noops(self):
        self.enable_notices("leave")
        self.bind()
        self.notify.reset_mock()
        version = self.version()
        await self.events.handle_member_leave(self.bot, departure(group=OTHER_GROUP))
        self.send_group.assert_not_awaited()
        await self.events.handle_member_leave(self.bot, departure(user=MEMBER))
        self.assert_notice("成员已退出群聊。")
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_repeated_departure_does_not_touch_new_binding(self):
        self.enable_notices("leave")
        self.bind()
        event = departure()
        self.notify.reset_mock()
        await asyncio.gather(*(self.events.handle_member_leave(self.bot, event) for _ in range(2)))
        self.notify.assert_called_once_with()
        self.send_group.assert_awaited_once()
        self.bind(name="NewSteve", timestamp=DEPARTED_AT)
        version = self.version()
        self.notify.reset_mock()
        await self.events.handle_member_leave(self.bot, event)
        self.assertEqual(self.binding(), "NewSteve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()
        self.send_group.assert_awaited_once()

    async def test_old_departure_after_restart_does_not_touch_new_binding(self):
        self.bind(timestamp=DEPARTED_AT + 1)
        version = self.version()
        self.notify.reset_mock()
        await self.events.handle_member_leave(self.bot, departure())
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_bot_removed_does_not_revoke_and_disables_audit(self):
        self.bind()
        self.bind(BOT, "BotUser")
        version = self.version()
        self.notify.reset_mock()
        with patch.object(self.events.logger, "error") as error:
            await self.events.handle_member_leave(self.bot, departure("kick_me", user=BOT))
        error.assert_called_once()
        with self.mock_roster(member_rows(BOT, MEMBER)) as request:
            self.assertIsNone(await self.events.reconcile_members(self.bot))
        request.assert_not_called()
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.binding(BOT), "BotUser")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()
        self.send_group.assert_not_awaited()

    async def test_failed_group_message_does_not_undo_or_repeat_revocation(self):
        self.enable_notices("leave")
        self.bind()
        version = self.version()
        self.notify.reset_mock()

        async def fail_after_commit(**_):
            self.assertIsNone(self.binding())
            self.assertGreater(self.version(), version)
            raise ActionFailed(retcode=100)

        self.send_group.side_effect = fail_after_commit
        await self.events.handle_member_leave(self.bot, departure())
        committed_version = self.version()
        await self.events.handle_member_leave(self.bot, departure())
        self.assertIsNone(self.binding())
        self.assertEqual(self.version(), committed_version)
        self.notify.assert_called_once_with()
        self.send_group.assert_awaited_once()

    async def test_join_in_other_group_sends_nothing(self):
        self.enable_notices("join")
        await self.events.handle_member_join(self.bot, arrival(group=OTHER_GROUP))
        self.send_group.assert_not_awaited()

    async def test_join_switch_preserves_membership_tracking_and_custom_welcome_uses_structured_at(self):
        generation = self.events.membership_generation
        with self.configure("events: {join: {enabled: false}}"):
            await self.events.handle_member_join(self.bot, arrival())
        self.send_group.assert_not_awaited()
        self.assertEqual(self.events.membership_generation, generation + 1)
        with self.configure('events: {join: {enabled: true, message: "你好 {member}！[CQ:at,qq=all]"}}'):
            await self.events.handle_member_join(self.bot, arrival())
        self.send_group.assert_awaited_once()
        message = self.send_group.call_args.kwargs["message"]
        self.assertEqual([segment.type for segment in message], ["text", "at", "text"])
        self.assertEqual(message[1].data["qq"], str(ABSENT))
        self.assertEqual(message.extract_plain_text(), "你好 ！[CQ:at,qq=all]")
        self.assertIsNone(self.binding())
        self.notify.assert_not_called()

    async def test_departure_switches_are_independent_and_never_skip_revocation(self):
        for enabled in ("leave", "kick"):
            disabled = "kick" if enabled == "leave" else "leave"
            with self.subTest(enabled=enabled), self.configure(f"""
events:
  {enabled}: {{enabled: true, revoked: 已完成自定义撤权通知}}
  {disabled}: {{enabled: false}}
"""):
                for kind in (enabled, disabled):
                    self.bind()
                    version = self.version()
                    self.send_group.reset_mock()
                    self.notify.reset_mock()
                    await self.events.handle_member_leave(self.bot, departure(kind))
                    self.assertIsNone(self.binding())
                    self.assertGreater(self.version(), version)
                    self.notify.assert_called_once_with()
                    if kind == enabled:
                        self.assert_notice("已完成自定义撤权通知")
                    else:
                        self.send_group.assert_not_awaited()
            self.events.recent_departures.clear()

    async def test_departure_chooses_no_binding_or_transaction_failure_template(self):
        with self.configure("""
events:
  leave: {enabled: true, revoked: leave success, no_binding: leave empty, failed: leave failed}
  kick: {enabled: true, revoked: kick success, no_binding: kick empty, failed: kick failed}
"""):
            for kind in ("leave", "kick"):
                self.send_group.reset_mock()
                await self.events.handle_member_leave(self.bot, departure(kind, user=MEMBER))
                self.assert_notice(f"{kind} empty")
                self.bind()
                version = self.version()
                self.notify.reset_mock()
                self.send_group.reset_mock()
                with patch.object(binding_service, "update_auth_version", side_effect=sqlite3.IntegrityError):
                    await self.events.handle_member_leave(self.bot, departure(kind))
                    await self.events.handle_member_leave(self.bot, departure(kind))
                self.assert_notice(f"{kind} failed")
                self.assertEqual(self.binding(), "Steve")
                self.assertEqual(self.version(), version)
                self.notify.assert_not_called()
                self.send_group.reset_mock()
                await self.events.handle_member_leave(self.bot, departure(kind))
                self.assert_notice(f"{kind} success")
                self.assertIsNone(self.binding())
                self.notify.assert_called_once_with()

    async def test_all_notices_disabled_by_default_still_revoke_on_leave_and_kick(self):
        await self.events.handle_member_join(self.bot, arrival())
        for kind in ("leave", "kick"):
            with self.subTest(kind=kind):
                self.bind()
                version = self.version()
                self.notify.reset_mock()
                await self.events.handle_member_leave(self.bot, departure(kind))
                self.assertIsNone(self.binding())
                self.assertGreater(self.version(), version)
                self.notify.assert_called_once_with()
        self.send_group.assert_not_awaited()

    async def test_failed_transaction_rolls_back_and_same_event_can_retry(self):
        self.bind()
        version = self.version()
        self.notify.reset_mock()
        with patch.object(binding_service, "update_auth_version", side_effect=sqlite3.IntegrityError):
            await self.events.handle_member_leave(self.bot, departure())
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()
        await self.events.handle_member_leave(self.bot, departure())
        self.assertIsNone(self.binding())
        self.notify.assert_called_once_with()

    async def test_commit_failure_does_not_notify_or_persist_revocation(self):
        self.bind()
        version = self.version()
        self.notify.reset_mock()
        connect = sqlite3.connect
        with closing(connect(self.path)) as reader:
            reader.execute("PRAGMA journal_mode = DELETE")
            reader.execute("BEGIN")
            reader.execute("SELECT version FROM auth_state").fetchone()
            with patch.object(binding_service.sqlite3, "connect", side_effect=lambda path, **_: connect(path, timeout=0.05)):
                await self.events.handle_member_leave(self.bot, departure())
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_connect_audits_offline_departures_and_repeated_audit_is_idempotent(self):
        self.bind()
        self.bind(204, "Second")
        self.bind(MEMBER, "Alex")
        self.bind(name="Other", group=OTHER_GROUP)
        self.notify.reset_mock()
        version = self.version()
        self.assertIn(self.events.reconcile_on_connect, {hook.call for hook in self.driver._bot_connection_hook})
        with self.mock_roster(member_rows(BOT, MEMBER)) as request:
            await self.events.reconcile_on_connect(self.bot)
            self.assertEqual(request.await_count, 3)
        self.assertGreater(self.version(), version)
        self.assertIsNone(self.binding())
        self.assertIsNone(self.binding(204))
        self.assertEqual(self.binding(MEMBER), "Alex")
        self.assertEqual(self.binding(group=OTHER_GROUP), "Other")
        self.notify.assert_called_once_with()
        version = self.version()
        with self.mock_roster(member_rows(BOT, MEMBER)):
            self.assertEqual(await self.events.reconcile_members(self.bot), 0)
        self.assertEqual(self.version(), version)
        self.notify.assert_called_once_with()

    async def test_failed_member_api_keeps_all_bindings(self):
        self.bind()
        version = self.version()
        self.notify.reset_mock()
        with patch.object(self.bot, "call_api", AsyncMock(side_effect=ActionFailed(retcode=100))):
            self.assertIsNone(await self.events.reconcile_members(self.bot))
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_incomplete_or_inconsistent_rosters_never_revoke(self):
        self.bind()
        version = self.version()
        self.notify.reset_mock()
        valid = member_rows(BOT, MEMBER)
        for label, first, second, count, detail_group in (
            ("empty", [], None, 0, GROUP),
            ("null", None, None, 2, GROUP),
            ("paginated object", {"members": valid, "next_token": "next"}, None, 2, GROUP),
            ("partial array", valid, None, 3, GROUP),
            ("same count, different members", valid, member_rows(BOT, ABSENT), 2, GROUP),
            ("duplicate member", member_rows(BOT, BOT), None, 2, GROUP),
            ("bot absent", member_rows(MEMBER, ABSENT), None, 2, GROUP),
            ("wrong group", [*valid, {"group_id": OTHER_GROUP, "user_id": 204}], None, 3, GROUP),
            ("wrong group metadata", valid, None, 2, OTHER_GROUP),
            ("invalid member count", valid, None, "2", GROUP),
        ):
            with self.subTest(label=label), self.mock_roster(first, second, count=count, detail_group=detail_group):
                self.assertIsNone(await self.events.reconcile_members(self.bot))
                self.assertEqual(self.binding(), "Steve")
                self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_reconcile_does_not_delete_a_binding_created_during_member_queries(self):
        # A compatible legacy table can reuse the highest row ID after deletion.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'offline_bindings'",
            ).fetchone()[0]
            conn.execute("DROP TABLE offline_bindings")
            conn.execute(ddl.replace(" AUTOINCREMENT", ""))
            for index, columns in database.ACTIVE_INDEXES.items():
                conn.execute(f"CREATE UNIQUE INDEX {index} ON offline_bindings ({', '.join(columns)}) WHERE status = 'ACTIVE'")
        database.init_database(self.path)
        self.bind()
        checked_version, checked = binding_service.binding_audit_snapshot(str(GROUP))
        self.assertIsNotNone(binding_service.unbind_player(str(GROUP), str(ABSENT)))
        self.bind(name="NewSteve")
        self.assertEqual(binding_service.binding_audit_snapshot(str(GROUP))[1], checked)
        version = self.version()
        self.notify.reset_mock()
        self.assertIsNone(binding_service.revoke_checked_bindings(str(GROUP), checked_version, checked))
        self.assertEqual(self.binding(), "NewSteve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_manual_reconciliation_requires_superuser_and_target_group(self):
        def message(user, group):
            text = "/授权对账"
            return GroupMessageEvent.model_validate({
                "time": DEPARTED_AT, "self_id": BOT, "post_type": "message", "message_type": "group",
                "sub_type": "normal", "message_id": 1, "group_id": group, "user_id": user,
                "message": Message(text), "original_message": Message(text), "raw_message": text,
                "font": 0, "sender": {},
            })

        command = self.events.reconcile
        self.assertTrue(await command.permission(self.bot, message(MEMBER, GROUP)))
        self.assertFalse(await command.permission(self.bot, message(ABSENT, GROUP)))
        for group in (GROUP, OTHER_GROUP):
            event, state = message(MEMBER, group), {}
            TrieRule.get_value(self.bot, event, state)
            self.assertEqual(await command.rule(self.bot, event, state), group == GROUP)

    async def test_membership_event_during_audit_invalidates_even_matching_rosters(self):
        self.bind()
        self.notify.reset_mock()
        version = self.version()
        roster = member_rows(BOT, MEMBER)

        async def api(action, **_):
            if action == "get_group_detail_info":
                await self.events.handle_member_join(self.bot, GroupIncreaseNoticeEvent(
                    time=DEPARTED_AT, self_id=BOT, post_type="notice", notice_type="group_increase",
                    sub_type="approve", group_id=GROUP, user_id=ABSENT, operator_id=MEMBER,
                ))
                return {"group_id": GROUP, "member_count": len(roster)}
            return roster

        with patch.object(self.bot, "call_api", AsyncMock(side_effect=api)):
            self.assertIsNone(await self.events.reconcile_members(self.bot))
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_connect_audit_retries_once_after_failed_member_lookup(self):
        self.bind()
        self.notify.reset_mock()
        roster = member_rows(BOT, MEMBER)
        responses = [ActionFailed(retcode=100), roster, {"group_id": GROUP, "member_count": 2}, roster]
        with patch.object(self.events, "MEMBER_CHECK_RETRY_DELAY", 0), patch.object(
            self.bot, "call_api", AsyncMock(side_effect=responses)
        ) as request:
            await self.events.reconcile_on_connect(self.bot)
        self.assertEqual(request.await_count, 4)
        self.assertIsNone(self.binding())
        self.notify.assert_called_once_with()

    def group_message(self, text, *, user=ABSENT, group=GROUP):
        return GroupMessageEvent.model_validate({
            "time": DEPARTED_AT, "self_id": BOT, "post_type": "message", "message_type": "group",
            "sub_type": "normal", "message_id": 1, "group_id": group, "user_id": user,
            "message": Message(text), "original_message": Message(text), "raw_message": text,
            "font": 0, "sender": {},
        })

    async def command_reply(self, text, **event_fields):
        self.reply.reset_mock()
        await self.bot.handle_event(self.group_message(text, **event_fields))
        self.reply.assert_awaited_once()
        return self.reply.call_args.kwargs["message"]

    async def test_bilingual_binding_commands_share_behavior(self):
        replies = []
        for bind, query, unbind in (("/绑定", "/我的绑定", "/解绑"), ("/bind", "/binding", "/unbind")):
            replies.append((
                str(await self.command_reply(f"{bind} Steve")),
                str(await self.command_reply(query)),
                str(await self.command_reply(unbind)),
            ))
            self.assertIsNone(self.binding())
        self.assertEqual(replies[0], replies[1])
        self.assertIn("绑定成功", replies[0][0])
        self.assertIn("Steve", replies[0][1])
        self.assertIn("解绑成功", replies[0][2])

    async def test_bilingual_help_ping_and_reconcile_dispatch(self):
        self.assertEqual(await self.command_reply("/帮助"), await self.command_reply("/help"))
        with patch.object(self.status, "check_fastapi", return_value=runtime_status.HealthResult(503, 24)):
            ping = await self.command_reply("/ping")
            self.assertEqual(ping, await self.command_reply("/认证测试"))
            self.assertIn("HTTP 503\nLatency: 24 ms", ping.extract_plain_text())
        with patch.object(self.events, "reconcile_members", AsyncMock(return_value=0)) as reconcile:
            self.assertEqual(
                await self.command_reply("/reconcile", user=MEMBER),
                await self.command_reply("/授权对账", user=MEMBER),
            )
            self.assertEqual(reconcile.await_count, 2)
            self.reply.reset_mock()
            await self.bot.handle_event(self.group_message("/reconcile"))
            self.reply.assert_not_awaited()
            self.assertEqual(reconcile.await_count, 2)

    async def test_ping_failure_reply_explains_timeout_or_connection_failure(self):
        for reason in ("Timeout", "Connection failed"):
            with patch.object(self.status, "check_fastapi", return_value=runtime_status.HealthResult(None, 12, reason)):
                message = await self.command_reply("/ping")
                self.assertIn(f"FastAPI Health: {reason}\nLatency: 12 ms", message.extract_plain_text())

    async def test_custom_binding_replies_share_templates_and_interpolate_the_saved_name(self):
        with self.configure("""
commands:
  bind:
    usage: 请提供游戏名
    success: "已绑定 {mc_name}"
    invalid_name: 自定义格式错误
    already_bound: "已有 {mc_name}"
    name_taken: 自定义名称占用
  binding: {success: "查询到 {mc_name}", not_bound: 自定义未绑定}
  unbind: {success: "已解绑 {mc_name}", not_bound: 自定义无法解绑}
"""):
            for bind, query, unbind in (("/绑定", "/我的绑定", "/解绑"), ("/bind", "/binding", "/unbind")):
                for text, expected in (
                    (bind, "请提供游戏名"),
                    (f"{bind} invalid-name", "自定义格式错误"),
                    (f"{bind} Steve", "已绑定 Steve"),
                    (f"{bind} Alex", "已有 Steve"),
                    (query, "查询到 Steve"),
                    (unbind, "已解绑 Steve"),
                    (query, "自定义未绑定"),
                    (unbind, "自定义无法解绑"),
                ):
                    with self.subTest(text=text):
                        reply = await self.command_reply(text)
                        self.assertEqual(reply.extract_plain_text(), expected)
                self.bind(MEMBER, "Alex")
                reply = await self.command_reply(f"{bind} alex")
                self.assertEqual(reply.extract_plain_text(), "自定义名称占用")
                await self.command_reply(unbind, user=MEMBER)

    async def test_custom_help_ping_and_reconcile_replies(self):
        with self.configure("""
commands:
  help: {message: "自定义帮助 [CQ:at,qq=all]"}
  ping:
    success: "HTTP {status_code} / {latency_ms} ms"
    timeout: "超时 {latency_ms} ms"
    connection_error: "连接失败 {latency_ms} ms"
    error: "HTTP 异常 {latency_ms} ms"
  reconcile: {success: "撤销 {count} 条", failed: 自定义对账失败, busy: 自定义对账忙}
"""):
            for command in ("/帮助", "/help"):
                reply = await self.command_reply(command)
                self.assertEqual([segment.type for segment in reply], ["text"])
                self.assertEqual(reply.extract_plain_text(), "自定义帮助 [CQ:at,qq=all]")
            for code, error, expected in (
                (503, None, "HTTP 503 / 24 ms"),
                (None, "Timeout", "超时 24 ms"),
                (None, "Connection failed", "连接失败 24 ms"),
                (None, "Invalid HTTP response", "HTTP 异常 24 ms"),
            ):
                result = runtime_status.HealthResult(code, 24.2, error)
                with patch.object(self.status, "check_fastapi", return_value=result):
                    for command in ("/认证测试", "/ping"):
                        self.assertEqual((await self.command_reply(command)).extract_plain_text(), expected)
            for count, expected in ((3, "撤销 3 条"), (None, "自定义对账失败")):
                with patch.object(self.events, "reconcile_members", AsyncMock(return_value=count)):
                    for command in ("/授权对账", "/reconcile"):
                        reply = await self.command_reply(command, user=MEMBER)
                        self.assertEqual(reply.extract_plain_text(), expected)
            async with self.events.reconcile_lock:
                reply = await self.command_reply("/reconcile", user=MEMBER)
                self.assertEqual(reply.extract_plain_text(), "自定义对账忙")

    async def test_info_requires_superuser_and_sends_png_for_both_aliases(self):
        with (
            patch.object(runtime_status, "check_fastapi", return_value=runtime_status.HealthResult(200, 15)),
            patch.object(self.bot, "call_api", AsyncMock(return_value={"user_id": BOT, "nickname": "夜萤助手"})) as login,
            patch.object(self.status, "render_status", wraps=self.status.render_status) as render,
        ):
            await self.status.remember_connection(self.bot)
            for alias in ("/信息", "/info"):
                self.reply.reset_mock()
                await self.bot.handle_event(self.group_message(alias))
                self.reply.assert_not_awaited()
                image = await self.command_reply(alias, user=MEMBER)
                self.assertEqual(image.type, "image")
                prefix, payload = image.data["file"].split("://", 1)
                self.assertEqual(prefix, "base64")
                with Image.open(BytesIO(base64.b64decode(payload))) as decoded:
                    self.assertEqual(decoded.format, "PNG")
                    decoded.verify()
            await self.status.forget_connection(self.bot)
            self.assertNotIn(self.bot, self.status.connected_at)
        self.assertEqual(login.await_args_list, [call("get_login_info"), call("get_login_info")])
        self.assertEqual(
            [(item.kwargs["qq_id"], item.kwargs["nickname"]) for item in render.call_args_list],
            [(str(BOT), "夜萤助手")] * 2,
        )
        self.status.get_avatar.assert_has_calls([call(str(BOT)), call(str(BOT))])

    async def test_info_login_failure_still_sends_image_with_current_account_id(self):
        with (
            patch.object(runtime_status, "check_fastapi", return_value=runtime_status.HealthResult(200, 15)),
            patch.object(self.bot, "call_api", AsyncMock(side_effect=TimeoutError)),
            patch.object(self.status, "render_status", wraps=self.status.render_status) as render,
        ):
            image = await self.command_reply("/info", user=MEMBER)
        self.assertEqual(image.type, "image")
        self.assertEqual(render.call_args.kwargs["qq_id"], str(BOT))
        self.assertIsNone(render.call_args.kwargs["nickname"])

    async def test_info_ignores_nickname_from_a_different_account(self):
        with patch.object(self.bot, "call_api", AsyncMock(return_value={"user_id": MEMBER, "nickname": "other"})):
            self.assertIsNone(await self.status._nickname(self.bot))

    async def test_commands_in_other_group_do_not_respond_and_status_has_no_matcher(self):
        for text in (
            "/绑定 Steve", "/bind Steve", "/解绑", "/unbind", "/我的绑定", "/binding",
            "/认证测试", "/ping", "/帮助", "/help", "/授权对账", "/reconcile", "/信息", "/info",
        ):
            await self.bot.handle_event(self.group_message(text, group=OTHER_GROUP, user=MEMBER))
        await self.bot.handle_event(self.group_message("/status", user=MEMBER))
        self.reply.assert_not_awaited()
        self.notify.assert_not_called()
