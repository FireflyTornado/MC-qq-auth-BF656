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
from unittest.mock import AsyncMock, patch

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
        self.enterContext(patch.object(runtime_status, "_outcomes", {}))
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
        self.send_group.assert_awaited_once_with(
            group_id=GROUP, message="成员已退出群聊。Minecraft 授权已自动撤销。",
        )
        await self.events.handle_member_join(self.bot, GroupIncreaseNoticeEvent(
            time=DEPARTED_AT + 1, self_id=BOT, post_type="notice", notice_type="group_increase",
            sub_type="approve", group_id=GROUP, user_id=ABSENT, operator_id=MEMBER,
        ))
        self.assertIsNone(self.binding())
        welcome = self.send_group.call_args.kwargs["message"]
        self.assertEqual(welcome[0].type, "at")
        self.assertEqual(welcome[0].data["qq"], str(ABSENT))
        for command in ("/绑定 游戏ID", "/bind 游戏ID", "/帮助", "/help"):
            self.assertIn(command, welcome.extract_plain_text())

    async def test_kick_revokes_the_member_not_the_operator(self):
        self.bind()
        self.bind(MEMBER, "Alex")
        self.notify.reset_mock()
        await self.events.handle_member_leave(self.bot, departure("kick"))
        self.assertIsNone(self.binding())
        self.assertEqual(self.binding(MEMBER), "Alex")
        self.notify.assert_called_once_with()
        self.send_group.assert_awaited_once_with(
            group_id=GROUP, message="成员已被移出群聊。Minecraft 授权已自动撤销。",
        )

    async def test_other_group_and_unbound_departures_are_noops(self):
        self.bind()
        self.notify.reset_mock()
        version = self.version()
        await self.events.handle_member_leave(self.bot, departure(group=OTHER_GROUP))
        self.send_group.assert_not_awaited()
        await self.events.handle_member_leave(self.bot, departure(user=MEMBER))
        self.send_group.assert_awaited_once_with(group_id=GROUP, message="成员已退出群聊。")
        self.assertEqual(self.binding(), "Steve")
        self.assertEqual(self.version(), version)
        self.notify.assert_not_called()

    async def test_repeated_departure_does_not_touch_new_binding(self):
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
        self.bind()
        version = self.version()
        self.notify.reset_mock()

        async def fail_after_commit(**_):
            self.assertIsNone(self.binding())
            self.assertGreater(self.version(), version)
            raise ActionFailed(retcode=100)

        self.send_group.side_effect = fail_after_commit
        await self.events.handle_member_leave(self.bot, departure())
        await self.events.handle_member_leave(self.bot, departure())
        self.assertIsNone(self.binding())
        self.notify.assert_called_once_with()
        self.send_group.assert_awaited_once()

    async def test_join_in_other_group_sends_nothing(self):
        await self.events.handle_member_join(self.bot, GroupIncreaseNoticeEvent(
            time=DEPARTED_AT, self_id=BOT, post_type="notice", notice_type="group_increase",
            sub_type="approve", group_id=OTHER_GROUP, user_id=ABSENT, operator_id=MEMBER,
        ))
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
            self.assertIn("HTTP 503\nLatency: 24 ms", ping)
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
                self.assertIn(f"FastAPI Health: {reason}\nLatency: 12 ms", message)

    async def test_info_requires_superuser_and_sends_png_for_both_aliases(self):
        with patch.object(runtime_status, "check_fastapi", return_value=runtime_status.HealthResult(200, 15)):
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

    async def test_commands_in_other_group_do_not_respond_and_status_has_no_matcher(self):
        for text in (
            "/绑定 Steve", "/bind Steve", "/解绑", "/unbind", "/我的绑定", "/binding",
            "/认证测试", "/ping", "/帮助", "/help", "/授权对账", "/reconcile", "/信息", "/info",
        ):
            await self.bot.handle_event(self.group_message(text, group=OTHER_GROUP, user=MEMBER))
        await self.bot.handle_event(self.group_message("/status", user=MEMBER))
        self.reply.assert_not_awaited()
        self.notify.assert_not_called()
