import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

BOT_DIR = Path(__file__).resolve().parent
ENV_FILE = BOT_DIR / ".env"
DEFAULT_NOTIFY_URL = "http://127.0.0.1:18765/internal/mc/notify"
MAX_TOKEN_LENGTH = 4096
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


class ConfigError(RuntimeError):
    """A configuration error whose message can be shown without revealing values."""


def _normalize(values: dict[str, str | None]) -> dict[str, str | None]:
    normalized = {key.upper(): value for key, value in values.items()}
    # Recognize the adapter's v11 aliases within each configuration source.
    for key in ("ACCESS_TOKEN", "WS_URLS"):
        alias = f"ONEBOT_V11_{key}"
        if alias in normalized:
            normalized[f"ONEBOT_{key}"] = normalized[alias]
    return normalized


def load_config() -> dict[str, str | None]:
    """Read the project dotenv file without mutating the process environment."""
    try:
        file_values = dotenv_values(ENV_FILE)
    except (OSError, UnicodeError):
        raise ConfigError("无法读取 bot/.env，请检查权限和 UTF-8 编码") from None
    return {**_normalize(file_values), **_normalize(dict(os.environ))}


def info_layout(config: dict[str, str | None]) -> str:
    layout = (config.get("INFO_LAYOUT") or "").strip() or "default"
    if layout not in ("default", "custom"):
        raise ConfigError("INFO_LAYOUT 必须是 default 或 custom")
    return layout


def is_bearer_token(value: str) -> bool:
    return len(value) <= MAX_TOKEN_LENGTH and TOKEN_PATTERN.fullmatch(value) is not None


def require_token(config: dict[str, str | None], key: str) -> str:
    value = config.get(key)
    if not value:
        raise ConfigError(f"缺少必需配置 {key}")
    if value == f"REPLACE_WITH_{key}" or not is_bearer_token(value):
        raise ConfigError(f"{key} 必须是有效的 ASCII Bearer Token，长度最多 {MAX_TOKEN_LENGTH}")
    return value


def target_group_id(config: dict[str, str | None]) -> int:
    value = config.get("TARGET_GROUP_ID")
    if not value:
        raise ConfigError("缺少必需配置 TARGET_GROUP_ID")
    value = value.strip()
    if value.isascii() and value.isdecimal():
        try:
            group_id = int(value)
        except ValueError:
            pass
        else:
            if group_id > 0:
                return group_id
    raise ConfigError("TARGET_GROUP_ID 必须是正整数")


def onebot_ws_urls(config: dict[str, str | None]) -> list[str]:
    raw = config.get("ONEBOT_WS_URLS")
    if raw is None:
        return []
    try:
        urls = json.loads(raw)
    except ValueError:
        urls = None
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ConfigError("ONEBOT_WS_URLS 必须是 WebSocket URL 的 JSON 数组")
    return urls


def notify_url(config: dict[str, str | None]) -> str:
    value = config.get("MC_NOTIFY_URL", DEFAULT_NOTIFY_URL)
    if value and value.isascii() and "\\" not in value and not any(
        ord(char) <= 32 or ord(char) == 127 for char in value
    ):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            pass
        else:
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and port != 0
            ):
                return value
    raise ConfigError("MC_NOTIFY_URL 必须是有效的 HTTP(S) URL，且不包含用户凭据或片段")
