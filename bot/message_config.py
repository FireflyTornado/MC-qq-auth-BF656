import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml
from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from app_config import BOT_DIR


MESSAGE_FILE = BOT_DIR / "messages.yml"
MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 1000

DEFAULTS = MappingProxyType({
    "events.join.enabled": False,
    "events.join.message": (
        "{member} 欢迎加入！请用 /绑定 游戏ID 或 /bind 游戏ID 绑定 Minecraft 游戏 ID。\n"
        "发送 /帮助 或 /help 查看指令。"
    ),
    "events.leave.enabled": False,
    "events.leave.revoked": "成员已退出群聊。Minecraft 授权已自动撤销。",
    "events.leave.no_binding": "成员已退出群聊。",
    "events.leave.failed": "成员已退出群聊。Minecraft 授权撤销暂未确认。",
    "events.kick.enabled": False,
    "events.kick.revoked": "成员已被移出群聊。Minecraft 授权已自动撤销。",
    "events.kick.no_binding": "成员已被移出群聊。",
    "events.kick.failed": "成员已被移出群聊。Minecraft 授权撤销暂未确认。",
    "commands.bind.usage": "用法：/绑定 Minecraft玩家名",
    "commands.bind.success": "绑定成功！\nMinecraft ID：{mc_name}\n你的绑定记录已保存。",
    "commands.bind.invalid_name": "游戏 ID 格式不正确。\n仅支持 3～16 位英文字母、数字和下划线。",
    "commands.bind.already_bound": "你已经绑定了游戏 ID：{mc_name}\n如需更换，请先使用 /解绑",
    "commands.bind.name_taken": "该游戏 ID 已经被绑定，无法重复绑定。",
    "commands.bind.error": "绑定服务发生错误，请稍后重试",
    "commands.unbind.success": "解绑成功！\n原 Minecraft ID：{mc_name}\n绑定记录已删除。",
    "commands.unbind.not_bound": "你当前没有有效的绑定记录。",
    "commands.unbind.error": "解绑失败，请稍后重试",
    "commands.binding.success": "当前绑定的 Minecraft ID：{mc_name}",
    "commands.binding.not_bound": "你还没有绑定 Minecraft 游戏 ID。\n请使用 /绑定 玩家名",
    "commands.binding.error": "查询失败，请稍后重试",
    "commands.ping.success": "[MC-QQ-Auth] Ping\nFastAPI Health: HTTP {status_code}\nLatency: {latency_ms} ms",
    "commands.ping.timeout": "[MC-QQ-Auth] Ping\nFastAPI Health: Timeout\nLatency: {latency_ms} ms",
    "commands.ping.connection_error": "[MC-QQ-Auth] Ping\nFastAPI Health: Connection failed\nLatency: {latency_ms} ms",
    "commands.ping.error": "[MC-QQ-Auth] Ping\nFastAPI Health: Invalid HTTP response\nLatency: {latency_ms} ms",
    "commands.reconcile.success": "授权对账完成，已撤销 {count} 条离群绑定。",
    "commands.reconcile.failed": "授权对账未完成，请检查群成员名单和服务日志。",
    "commands.reconcile.busy": "授权对账正在执行，请稍后重试。",
    "commands.help.message": (
        "[MC-QQ-Auth] 指令帮助\n"
        "/绑定 游戏ID 或 /bind 游戏ID · 绑定 Minecraft 游戏 ID\n"
        "/我的绑定 或 /binding · 查询自己的绑定\n"
        "/解绑 或 /unbind · 撤销自己的绑定\n"
        "/认证测试 或 /ping · 检查授权 API\n"
        "/帮助 或 /help · 显示本帮助\n"
        "超级用户专用\n"
        "/信息 或 /info · 查看状态图片\n"
        "/授权对账 或 /reconcile · 核对群成员与授权"
    ),
    "commands.info.busy": "状态图正在生成，请稍后重试。",
    "commands.info.error": "状态图暂不可用，请稍后重试。",
    "info.title": "MC-QQ-Auth",
    "info.footer": "MC-QQ-Auth",
    "info.labels.bot": "Bot",
    "info.labels.onebot": "OneBot",
    "info.labels.api": "FastAPI",
    "info.labels.database": "SQLite",
    "info.labels.system_uptime": "系统",
    "info.labels.bot_uptime": "Bot 进程",
    "info.labels.connection_uptime": "OneBot 连接",
    "info.labels.os": "操作系统",
    "info.labels.python": "Python",
    "info.labels.cpu_cores": "逻辑核心",
    "info.labels.memory": "内存",
    "info.labels.disk": "磁盘",
    "info.labels.auth_version": "Authorization version",
    "info.labels.bindings": "有效绑定",
    "info.labels.last_reconcile": "Last member reconciliation",
    "info.labels.last_notice": "Last authorization notice",
})
SECTIONS = {key.rsplit(".", depth)[0] for key in DEFAULTS for depth in range(1, key.count(".") + 1)}

FIELDS = {
    "events.join.message": {"member"},
    "commands.bind.success": {"mc_name"},
    "commands.bind.already_bound": {"mc_name"},
    "commands.unbind.success": {"mc_name"},
    "commands.binding.success": {"mc_name"},
    "commands.ping.success": {"status_code", "latency_ms"},
    "commands.ping.timeout": {"latency_ms"},
    "commands.ping.connection_error": {"latency_ms"},
    "commands.ping.error": {"latency_ms"},
    "commands.reconcile.success": {"count"},
}
TOKENS = re.compile(r"(\{\{|\}\}|\{[a-z_]+\}|[{}])")
UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


@dataclass(frozen=True)
class MessageConfig:
    values: Mapping[str, str | bool]

    def enabled(self, event: str) -> bool:
        return self.values[f"events.{event}.enabled"] is True

    def text(self, key: str) -> str:
        value = self.values[key]
        assert isinstance(value, str)
        return value

    def info_text(self, key: str) -> str:
        return self.text(key).replace("{{", "{").replace("}}", "}")

    def render(self, key: str, **values: str | int | MessageSegment | None) -> Message:
        template = self.text(key)
        message = _render(template, values)
        if message is None:
            logger.warning("[MC-QQ-AUTH] 消息变量缺失或渲染后过长，使用默认文案（{}）", key)
            return _render(DEFAULTS[key], values, use_default=True)
        return message


def _render(
    template: str, values: Mapping[str, str | int | MessageSegment | None], *, use_default: bool = False,
) -> Message | None:
    message = Message()
    text: list[str] = []
    length = 0
    for index, part in enumerate(TOKENS.split(template)):
        if index % 2:
            if part in ("{{", "}}"):
                part = part[0]
            else:
                value = values.get(part[1:-1])
                if value is None and not use_default:
                    return None
                if isinstance(value, MessageSegment):
                    if text:
                        message.append(MessageSegment.text("".join(text)))
                        text.clear()
                    message.append(value)
                    length += len(str(value))
                    continue
                part = "—" if value is None else UNSAFE_TEXT.sub("�", str(value))[:128]
        if part:
            text.append(part)
            length += len(part)
    if length > MAX_TEXT_LENGTH and not use_default:
        return None
    if text:
        message.append(MessageSegment.text("".join(text)))
    return message


def _apply_overrides(raw: object, values: dict[str, str | bool], prefix: str = "") -> None:
    if not isinstance(raw, dict):
        logger.warning("[MC-QQ-AUTH] 消息配置节应为映射，使用默认值（{}）", prefix or "根节点")
        return
    for name, value in raw.items():
        if not isinstance(name, str) or "." in name:
            key = ""
        else:
            key = f"{prefix}.{name}" if prefix else name
        if key in SECTIONS:
            _apply_overrides(value, values, key)
        elif key not in DEFAULTS:
            logger.warning("[MC-QQ-AUTH] 忽略未知消息配置项（{}）", prefix or "根节点")
        elif isinstance(DEFAULTS[key], bool):
            if type(value) is bool:
                values[key] = value
            else:
                logger.warning("[MC-QQ-AUTH] 消息通知开关应为布尔值，使用默认值（{}）", key)
        elif not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_LENGTH or UNSAFE_TEXT.search(value):
            logger.warning("[MC-QQ-AUTH] 消息文案无效或过长，使用默认值（{}）", key)
        elif any(
            token not in ("{{", "}}") and token[1:-1] not in FIELDS.get(key, ())
            for token in TOKENS.split(value)[1::2]
        ):
            logger.warning("[MC-QQ-AUTH] 消息占位符无效，使用默认值（{}）", key)
        else:
            values[key] = value


def load_messages(path: Path = MESSAGE_FILE) -> MessageConfig:
    values = dict(DEFAULTS)
    try:
        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError
        raw = yaml.safe_load(data.decode("utf-8-sig"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, OverflowError, RecursionError, yaml.YAMLError):
        # YAML errors can quote file content, so never log the exception itself.
        logger.error("[MC-QQ-AUTH] 无法读取或解析 messages.yml，使用默认配置")
    else:
        _apply_overrides(raw, values)
    return MessageConfig(MappingProxyType(values))


# NoneBot loads the plugins at startup. All handlers share this validated snapshot.
MESSAGES = load_messages()
