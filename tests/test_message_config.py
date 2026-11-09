import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import message_config as config


class MessageConfigTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "messages.yml"

    def load(self, source):
        self.path.write_text(source, encoding="utf-8")
        return config.load_messages(self.path)

    def test_missing_file_uses_builtins_without_warning(self):
        with patch.object(config.logger, "warning") as warning:
            snapshot = config.load_messages(self.path)
        warning.assert_not_called()
        self.assertEqual(snapshot.values, config.DEFAULTS)
        self.assertEqual(config.MESSAGE_FILE, BOT_DIR / "messages.yml")

    def test_complete_example_matches_all_defaults(self):
        with patch.object(config.logger, "warning") as warning, patch.object(config.logger, "error") as error:
            snapshot = config.load_messages(BOT_DIR / "messages.example.yml")
        warning.assert_not_called()
        error.assert_not_called()
        self.assertEqual(snapshot.values, config.DEFAULTS)
        import yaml
        raw = yaml.safe_load((BOT_DIR / "messages.example.yml").read_text(encoding="utf-8"))
        for key, default in config.DEFAULTS.items():
            value = raw
            for part in key.split("."):
                value = value[part]
            self.assertEqual(value, default, key)

    def test_partial_overrides_and_snapshot_isolation(self):
        snapshot = self.load("events: {leave: {enabled: false}}\ninfo: {title: 私服状态}")
        self.load("events: {kick: {enabled: false}}")
        self.assertFalse(snapshot.enabled("leave"))
        self.assertTrue(snapshot.enabled("join"))
        self.assertTrue(snapshot.enabled("kick"))
        self.assertEqual(snapshot.text("info.title"), "私服状态")
        self.assertEqual(snapshot.text("info.footer"), config.DEFAULTS["info.footer"])
        with self.assertRaises(TypeError):
            snapshot.values["info.title"] = "修改"

    def test_invalid_yaml_unsafe_tags_and_read_failures_log_without_contents(self):
        with patch.object(config.logger, "error") as error:
            for source in ("private_config_marker: [", "!!python/object/apply:builtins.str [private_config_marker]"):
                with self.subTest(source=source):
                    self.assertEqual(self.load(source).values, config.DEFAULTS)
            with patch.object(Path, "open", side_effect=PermissionError("private_config_marker")):
                self.assertEqual(config.load_messages(self.path).values, config.DEFAULTS)
        self.assertEqual(error.call_count, 3)
        self.assertNotIn("private_config_marker", repr(error.call_args_list))

    def test_bad_types_non_boolean_switch_and_unknown_keys_only_affect_own_fields(self):
        with patch.object(config.logger, "warning") as warning:
            snapshot = self.load("""
events:
  leave: {enabled: "false", failed: 私服撤权异常}
  kick: {enabled: 0}
  join: []
commands:
  bind: {usage: [invalid], success: "已绑定 {mc_name}", private_config_marker: 123}
info:
  labels: {api: null, database: 数据库}
""")
        self.assertEqual(warning.call_count, 6)
        self.assertNotIn("private_config_marker", repr(warning.call_args_list))
        self.assertTrue(snapshot.enabled("leave"))
        self.assertTrue(snapshot.enabled("kick"))
        self.assertTrue(snapshot.enabled("join"))
        self.assertEqual(snapshot.text("commands.bind.usage"), config.DEFAULTS["commands.bind.usage"])
        self.assertEqual(snapshot.text("events.leave.failed"), "私服撤权异常")
        self.assertEqual(snapshot.text("commands.bind.success"), "已绑定 {mc_name}")
        self.assertEqual(snapshot.info_label("FastAPI"), "FastAPI")
        self.assertEqual(snapshot.info_label("SQLite"), "数据库")

    def test_rejects_unknown_or_non_simple_placeholders(self):
        key = "commands.bind.success"
        for token in ("{member}", "{mc_name.__class__}", "{mc_name[0]}", "{mc_name!r}",
                      "{mc_name:}", "{mc_name:{count}}", "{mc_name", "mc_name}"):
            with self.subTest(token=token), patch.object(config.logger, "warning") as warning:
                snapshot = self.load(f'commands: {{bind: {{success: "{token}"}}}}')
                self.assertEqual(snapshot.text(key), config.DEFAULTS[key])
                warning.assert_called_once()
                self.assertNotIn(token, repr(warning.call_args))

    def test_length_limits_for_file_template_and_rendered_reply(self):
        key = "commands.bind.success"
        with patch.object(config.logger, "warning"), patch.object(config.logger, "error"):
            self.assertEqual(self.load("#" * (config.MAX_FILE_BYTES + 1)).values, config.DEFAULTS)
            long_text = "x" * (config.MAX_TEXT_LENGTH + 1)
            self.assertEqual(self.load(f"commands: {{bind: {{success: {long_text}}}}}").text(key), config.DEFAULTS[key])
            snapshot = self.load('commands: {bind: {success: "' + "{mc_name}" * 100 + '"}}')
            self.assertIn("绑定成功", snapshot.render(key, mc_name="SteveSteve1").extract_plain_text())

    def test_multiline_braces_and_cq_characters_stay_literal(self):
        snapshot = self.load("""
commands:
  bind:
    success: |-
      第一行 {{指南}} & [CQ:at,qq=all]
      第二行 {mc_name}
""")
        message = snapshot.render("commands.bind.success", mc_name="[CQ:image,file=injected]")
        self.assertEqual([segment.type for segment in message], ["text"])
        self.assertEqual(message.extract_plain_text(),
                         "第一行 {指南} & [CQ:at,qq=all]\n第二行 [CQ:image,file=injected]")

    def test_missing_placeholder_value_uses_default_with_safe_marker(self):
        snapshot = self.load('commands: {bind: {success: "custom {mc_name}"}}')
        with patch.object(config.logger, "warning") as warning:
            reply = snapshot.render("commands.bind.success", mc_name=None).extract_plain_text()
        warning.assert_called_once()
        self.assertEqual(reply, config.DEFAULTS["commands.bind.success"].replace("{mc_name}", "—"))
