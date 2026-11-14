import json
import re
import unicodedata
from dataclasses import dataclass

from app_config import ConfigError, require_token

SUDO_TIMEOUT = 30
SUDO_SUBMIT_PATH = "/internal/mc/sudo/submit"
SUDO_WS_PATH = "/internal/mc/sudo/ws"
MAX_COMMAND_LENGTH = 256
MAX_RESULT_LENGTH = 1500


@dataclass(frozen=True)
class SudoSettings:
    qq_ids: frozenset[str]
    bot_token: str
    plugin_token: str


def read_sudo_settings(config: dict[str, str | None]) -> SudoSettings | None:
    raw_ids = config.get("MC_SUDO_QQ_IDS")
    if raw_ids is None:
        return None
    try:
        ids = json.loads(raw_ids)
    except (ValueError, RecursionError):
        ids = None
    if not isinstance(ids, list) or any(
        not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None
        for value in ids
    ):
        raise ConfigError("MC_SUDO_QQ_IDS 必须为 QQ 号字符串的 JSON 数组")
    if not ids:
        return None
    bot_token = require_token(config, "MC_SUDO_BOT_TOKEN")
    plugin_token = require_token(config, "MC_SUDO_PLUGIN_TOKEN")
    if bot_token == plugin_token or {bot_token, plugin_token} & {
        config.get("MC_API_TOKEN"),
        config.get("MC_NOTIFY_TOKEN"),
        config.get("ONEBOT_ACCESS_TOKEN"),
    }:
        raise ConfigError("/sudo 凭据必须独立配置")
    return SudoSettings(frozenset(ids), bot_token, plugin_token)


def is_single_command(command: str) -> bool:
    return (
        0 < len(command) <= MAX_COMMAND_LENGTH
        and command == command.strip(" ")
        and not any(unicodedata.category(char)[0] == "C" or char in ("\u2028", "\u2029") for char in command)
    )
