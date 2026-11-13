import re
from base64 import b64encode
from bisect import bisect_right
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
from string import Template

from nonebot import logger
from PIL import Image, ImageFont, ImageOps
from resvg_py import svg_to_bytes

import message_config
from runtime_status import HealthResult, ResourceUsage, RuntimeStatus


WIDTH, HEIGHT = 720, 640
LEFT_CONTENT_WIDTH = 322
MINT, YELLOW, RED, GRAY = "#7ae7c2", "#f5ce79", "#ff8c91", "#8a9aa8"
STROKE_DASHES = {MINT: "none", YELLOW: "8 3", RED: "5 4", GRAY: "2 4"}
EMPTY = "—"
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@lru_cache(maxsize=1)
def _template() -> Template:
    return Template(Path(__file__).with_name("status_card.svg").read_text(encoding="utf-8"))


def _find_font(candidates: tuple[tuple[str, int], ...]) -> tuple[str, int] | None:
    for filename, index in candidates:
        try:
            font = ImageFont.truetype(filename, 20, index=index)
        except OSError:
            continue
        return str(font.path), index
    return None


@lru_cache(maxsize=2)
def _font_source(bold: bool) -> tuple[str, int] | None:
    weight = "Bold" if bold else "Regular"
    source = _find_font((
        (f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc", 2),
        ("msyhbd.ttc" if bold else "msyh.ttc", 0),
    ))
    if source is None:
        if bold:
            return _font_source(False)
        logger.warning("[MC-QQ-AUTH] 未找到中文字体，状态图使用英文备用标签")
    return source


@lru_cache(maxsize=2)
def _latin_source(bold: bool) -> tuple[str, int] | None:
    return _find_font((
        ("arialbd.ttf" if bold else "arial.ttf", 0),
        ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", 0),
        ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf", 0),
    ))


@lru_cache(maxsize=48)
def _font(source: tuple[str, int] | None, size: int) -> ImageFont.FreeTypeFont:
    if source is None:
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(source[0], size, index=source[1])


def _fit(value: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    value = " ".join(_INVALID_XML.sub("", value).split())
    if font.getlength(value) <= width:
        return value

    length = bisect_right(range(len(value)), width, key=lambda n: font.getlength(value[:n] + "…")) - 1
    return value[:max(0, length)] + "…"


def _duration(seconds: float | None, cjk: bool) -> str:
    if seconds is None:
        return EMPTY
    days, minutes = divmod(max(0, int(seconds)) // 60, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}天 {hours}小时" if cjk else f"{days}d {hours}h"
    if hours:
        return f"{hours}小时 {minutes}分" if cjk else f"{hours}h {minutes}m"
    return f"{minutes}分" if cjk else f"{minutes}m"


def _binary_color(value: bool | None) -> str:
    return GRAY if value is None else MINT if value else RED


def _api_color(health: HealthResult | None) -> str:
    if health is None:
        return GRAY
    if health.status_code == 200:
        return MINT
    if health.error == "Connection failed":
        return RED
    return YELLOW if health.status_code is not None or health.error else GRAY


def _meter(percent: float | None) -> tuple[str, float, str]:
    if percent is None:
        return EMPTY, 0, GRAY
    color = RED if percent >= 95 else YELLOW if percent >= 85 else MINT
    return f"{percent:.0f}%", round(LEFT_CONTENT_WIDTH * percent / 100, 2), color


def _usage(usage: ResourceUsage | None) -> str:
    if usage is None:
        return EMPTY
    gib = 1024 ** 3
    return f"{usage.used / gib:.1f} / {usage.total / gib:.1f} GiB"


def _avatar_uri(avatar: Image.Image | None) -> str:
    if avatar is None:
        return ""
    image = ImageOps.fit(avatar.convert("RGBA"), (108, 108), method=Image.Resampling.LANCZOS)
    with BytesIO() as output:
        image.save(output, format="PNG")
        return "data:image/png;base64," + b64encode(output.getvalue()).decode("ascii")


def render_status(
    status: RuntimeStatus, *, qq_id: str = "", nickname: str | None = None, avatar: Image.Image | None = None,
) -> bytes:
    cjk = _font_source(False) is not None
    regular = _font_source(False) if cjk else _latin_source(False)
    bold = (_font_source(True) if cjk else _latin_source(True)) or regular
    family = _font(regular, 16).getname()[0] if regular is not None else "sans-serif"
    messages = message_config.MESSAGES

    def text(value: str, size: int, width: int, fallback: str | None = None, *, strong: bool = False) -> str:
        if not cjk and fallback is not None and not value.isascii():
            value = fallback
        return _fit(value, _font(bold if strong else regular, size), width)

    def label(key: str) -> str:
        return messages.info_text(f"info.labels.{key}")

    onebot_name = label("connection_uptime")
    if onebot_name == message_config.DEFAULTS["info.labels.connection_uptime"]:
        onebot_name = label("onebot")
    onebot_uptime = ""
    if status.connected and status.connection_seconds is not None:
        onebot_uptime = _duration(status.connection_seconds, cjk)
    bot_name = label("bot_uptime")
    if bot_name == "Bot 进程":
        bot_name = "NoneBot2"

    api = status.api
    api_status = ""
    if api is not None:
        if api.status_code == 200:
            api_status = f"{api.latency_ms:.0f} ms"
        elif api.status_code is not None:
            api_status = api.summary
        elif api.error == "Timeout":
            api_status = "超时" if cjk else "Timeout"
        elif api.error == "Connection failed":
            api_status = "连接失败" if cjk else "Unreachable"
        elif api.error:
            api_status = "响应异常" if cjk else "Bad HTTP"
    ws_connected = None if api is None or api.minecraft_connections is None else api.minecraft_connections > 0

    game = status.minecraft
    game_color = _binary_color(game.reachable if game is not None else None)
    game_detail = ""
    if game is None:
        game_detail = "未探测" if cjk else "Not probed"
    elif game.reachable is None:
        game_color = YELLOW
        game_detail = "配置读取失败" if cjk else "Config error"
    elif game.reachable and game.online_players is not None and game.max_players is not None:
        game_detail = f"{game.online_players}/{game.max_players}"

    resources_label = "系统资源" if cjk else "System"
    resources_label_width = _font(bold, 19).getlength(resources_label)
    uptime_width = int(LEFT_CONTENT_WIDTH - resources_label_width - 16)
    uptime_label = label("system_uptime")
    if cjk:
        uptime_label = f"{uptime_label}运行"
    elif not uptime_label.isascii():
        uptime_label = "Uptime"
    uptime_suffix = f" · {_duration(status.system_seconds, cjk)}"
    uptime_label_width = int(uptime_width - _font(regular, 14).getlength(uptime_suffix))
    system_uptime = f"{text(uptime_label, 14, uptime_label_width)}{uptime_suffix}"
    system = status.system_name or EMPTY
    os_label = label("os")
    if os_label != message_config.DEFAULTS["info.labels.os"]:
        system = f"{os_label} {system}"
    python = status.python_version or EMPTY

    count = str(status.active_bindings) if status.active_bindings is not None else EMPTY
    max_count_size, max_count_width = 58, 168
    count_width = _font(bold, max_count_size).getlength(count)
    count_size = max(15, min(max_count_size, int(max_count_width * max_count_size / max(1, count_width))))
    count_text = text(count, count_size, max_count_width, strong=True)
    count_width = _font(bold, count_size).getlength(count_text)
    bindings_label_width = int(LEFT_CONTENT_WIDTH - count_width - 18)
    cores = status.cpu_cores if status.cpu_cores is not None else EMPTY
    local_time = status.checked_at.astimezone()
    offset = local_time.strftime("%z")

    fields = {
        "font_family": family,
        "title": text(messages.info_text("info.title"), 29, 485, "MC-QQ-Auth", strong=True),
        "page_label": "运行状态" if cjk else "Runtime status",
        "avatar": _avatar_uri(avatar),
        "avatar_display": "inline" if avatar is not None else "none",
        "account_color": _binary_color(status.connected),
        "nickname": text(nickname or "QQ 机器人", 23, 245, "QQ Bot", strong=True),
        "qq": text(f"QQ {qq_id or EMPTY}", 16, 245),
        "timestamp_label": "采集时间" if cjk else "Collected",
        "bindings_label": text(label("bindings"), 19, bindings_label_width, "Active bindings"),
        "bindings_count": count_text,
        "bindings_size": count_size,
        "services_label": "服务状态" if cjk else "Services",
        "system_uptime": system_uptime,
        "onebot_name": text(onebot_name, 14, 78 if onebot_uptime else 164, "OneBot"),
        "onebot_separator": " · " if onebot_uptime else "",
        "onebot_uptime": text(onebot_uptime, 14, 73),
        "bot_name": text(bot_name, 16, 110, "NoneBot2", strong=True),
        "bot_uptime": text(_duration(status.bot_seconds, cjk), 13, 110),
        "api_name": text(label("api"), 16, 110, "FastAPI", strong=True),
        "api_status": text(api_status, 14, 164),
        "minecraft_title_y": 528 if game_detail else 537,
        "minecraft_detail": text(game_detail, 13, 110),
        "sqlite_name": text(label("database"), 13, 79, "SQLite"),
        "resources_label": resources_label,
        "system": text(f"{system} · {label('python')} {python}", 15, LEFT_CONTENT_WIDTH,
                       f"{status.system_name or EMPTY} · Python {python}"),
        "meter_width": LEFT_CONTENT_WIDTH,
        "cpu_detail": text(f"{cores} {label('cpu_cores')}", 14, LEFT_CONTENT_WIDTH, f"{cores} cores"),
        "memory_label": text(label("memory"), 16, 185, "Memory", strong=True),
        "memory_detail": _usage(status.memory),
        "disk_label": text(label("disk"), 16, 185, "Disk", strong=True),
        "disk_detail": _usage(status.disk),
        "timestamp": f"{local_time:%Y-%m-%d %H:%M} UTC{offset[:3]}:{offset[3:5]}",
    }
    for key, color in (
        ("onebot", _binary_color(status.connected)),
        ("api", _api_color(api)),
        ("wss", _binary_color(ws_connected)),
        ("sqlite", _binary_color(status.sqlite_readable)),
        ("minecraft", game_color),
    ):
        fields[f"{key}_color"], fields[f"{key}_dash"] = color, STROKE_DASHES[color]
    for key, percent in (
        ("cpu", status.cpu_percent),
        ("memory", status.memory.percent if status.memory is not None else None),
        ("disk", status.disk.percent if status.disk is not None else None),
    ):
        fields[f"{key}_value"], fields[f"{key}_bar"], fields[f"{key}_color"] = _meter(percent)

    svg = _template().substitute({key: escape(str(value)) for key, value in fields.items()})
    font_files = list(dict.fromkeys(source[0] for source in (regular, bold) if source is not None))
    return svg_to_bytes(svg_string=svg, skip_system_fonts=bool(font_files), font_files=font_files)


def render_custom_status(
    status: RuntimeStatus, *, qq_id: str = "", nickname: str | None = None, avatar: Image.Image | None = None,
) -> bytes:
    """Use the existing card until the custom design is available."""
    return render_status(status, qq_id=qq_id, nickname=nickname, avatar=avatar)
