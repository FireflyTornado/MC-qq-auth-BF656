import json
import re
import unicodedata
from dataclasses import dataclass

from app_config import ConfigError, require_token

SUDO_TIMEOUT = 30
SUDO_SUBMIT_PATH = "/internal/mc/sudo/submit"
SUDO_WS_PATH = "/internal/mc/sudo/ws"
MAX_COMMAND_LENGTH = 256
MAX_RESULT_BYTES = 64 * 1024
MAX_RESULT_MESSAGE_BYTES = 512 * 1024


@dataclass(frozen=True)
class SudoOutputSettings:
    line_threshold: int = 10
    line_char_threshold: int = 0
    text_max_chars: int = 1500

    def use_file(self, output: str) -> bool:
        lines = output.splitlines()
        return (
            len(output) > self.text_max_chars
            or len(lines) > self.line_threshold
            or (self.line_char_threshold > 0 and any(len(line) > self.line_char_threshold for line in lines))
        )


def read_sudo_output_settings(config: dict[str, str | None]) -> SudoOutputSettings:
    def threshold(key: str, default: int, minimum: int) -> int:
        value = config.get(key)
        if value is None:
            return default
        try:
            if re.fullmatch(r"[0-9]+", value) and (number := int(value)) >= minimum:
                return number
        except ValueError:
            pass
        raise ConfigError(f"{key} 必须为大于等于 {minimum} 的整数")

    return SudoOutputSettings(
        threshold("MC_SUDO_FILE_LINE_THRESHOLD", 10, 1),
        threshold("MC_SUDO_FILE_LINE_CHAR_THRESHOLD", 0, 0),
        threshold("MC_SUDO_TEXT_MAX_CHARS", 1500, 1),
    )


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
