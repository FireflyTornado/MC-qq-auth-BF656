from bisect import bisect_right
from functools import lru_cache
from io import BytesIO
from math import cos, radians, sin

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

import message_config
from runtime_status import HealthResult, ResourceUsage, RuntimeStatus


WIDTH, HEIGHT = 880, 580
SCALE = 2
BG, CARD, BORDER = "#101923", "#192632", "#2b3d4b"
TEXT, MUTED, MINT = "#f0f6f8", "#a8bbcb", "#7ae7c2"
YELLOW, RED, GRAY = "#f5ce79", "#ff8c91", "#8a9aa8"
TRACK = "#2c4250"
EMPTY = "—"


@lru_cache(maxsize=2)
def _font_source(bold: bool) -> tuple[str, int] | None:
    weight = "Bold" if bold else "Regular"
    for source in (
        (f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc", 2),
        ("msyhbd.ttc" if bold else "msyh.ttc", 0),
    ):
        try:
            ImageFont.truetype(source[0], 20, index=source[1])
        except OSError:
            continue
        return source
    if bold:
        return _font_source(False)
    logger.warning("[MC-QQ-AUTH] 未找到中文字体，状态图使用英文备用标签")
    return None


@lru_cache(maxsize=40)
def _font(source: tuple[str, int] | None, size: int) -> ImageFont.FreeTypeFont:
    if source is None:
        return ImageFont.load_default(size=size * SCALE)
    return ImageFont.truetype(source[0], size * SCALE, index=source[1])


class _Canvas:
    def __init__(self) -> None:
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.messages = message_config.MESSAGES
        self.cjk = _font_source(False) is not None

    def text(self, x, y, value, size, color=TEXT, *, bold=False, anchor="lm", width=None, fallback=None):
        if not self.cjk and fallback is not None and not value.isascii():
            value = fallback
        value = " ".join(value.split())
        font = _font(_font_source(bold), size)
        if width is not None and self.draw.textbbox((0, 0), value, font=font)[2] > width * SCALE:
            def prefix_width(length):
                return self.draw.textbbox((0, 0), value[:length] + "…", font=font)[2]

            length = bisect_right(range(len(value)), width * SCALE, key=prefix_width) - 1
            value = value[:max(0, length)] + "…"
        self.draw.text((x * SCALE, y * SCALE), value, fill=color, font=font, anchor=anchor)

    def label(self, key: str) -> str:
        return self.messages.info_text(f"info.labels.{key}")

    def box(self, x1, y1, x2, y2, radius=17, fill=CARD, outline=BORDER):
        self.draw.rounded_rectangle(
            (x1 * SCALE, y1 * SCALE, x2 * SCALE, y2 * SCALE), radius * SCALE,
            fill=fill, outline=outline, width=SCALE,
        )

    def dot(self, x, y, radius, color):
        self.draw.ellipse(
            ((x - radius) * SCALE, (y - radius) * SCALE, (x + radius) * SCALE, (y + radius) * SCALE),
            fill=color,
        )

    def line(self, x1, y1, x2, y2, color=BORDER, width=1):
        self.draw.line((x1 * SCALE, y1 * SCALE, x2 * SCALE, y2 * SCALE), fill=color, width=width * SCALE)

    def light(self, x, y, state):
        self.dot(x, y, 5.5, state)
        if state == YELLOW:
            self.line(x, y - 2, x, y, BG)
            self.dot(x, y + 2, 0.65, BG)
        elif state == RED:
            self.line(x - 2, y - 2, x + 2, y + 2, BG)
            self.line(x + 2, y - 2, x - 2, y + 2, BG)
        elif state == GRAY:
            self.line(x - 2, y, x + 2, y, BG)

    def ring(self, x, y, percent):
        radius = 39
        bounds = ((x - radius) * SCALE, (y - radius) * SCALE, (x + radius) * SCALE, (y + radius) * SCALE)
        self.draw.ellipse(bounds, outline=TRACK, width=7 * SCALE)
        if percent is not None and percent > 0:
            end = percent * 3.6 - 90
            color = _resource_color(percent)
            self.draw.arc(bounds, -90, end, fill=color, width=7 * SCALE)
            mid = radius - 3.5
            self.dot(x, y - mid, 3.5, color)
            self.dot(x + mid * cos(radians(end)), y + mid * sin(radians(end)), 3.5, color)
        self.text(x, y, _percent(percent), 24, bold=True, anchor="mm")


def _resource_color(percent: float) -> str:
    return RED if percent >= 95 else YELLOW if percent >= 85 else MINT


def _percent(percent: float | None) -> str:
    return f"{percent:.0f}%" if percent is not None else EMPTY


def _usage(usage: ResourceUsage | None) -> str:
    if usage is None:
        return EMPTY
    gib = 1024 ** 3
    return f"{usage.used / gib:.1f} / {usage.total / gib:.1f} GiB"


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


def _binary_state(value: bool | None) -> str:
    return GRAY if value is None else MINT if value else RED


def _api_state(health: HealthResult | None) -> str:
    if health is None:
        return GRAY
    if health.status_code == 200:
        return MINT
    if health.error == "Connection failed":
        return RED
    return YELLOW if health.status_code is not None or health.error else GRAY


def _header(c: _Canvas) -> None:
    c.box(24, 20, 62, 58, 10, "#203a3b", "#2a4948")
    for points in (
        (33, 33, 43, 28), (43, 28, 53, 33), (53, 33, 53, 44),
        (53, 44, 43, 50), (43, 50, 33, 44), (33, 44, 33, 33),
        (33, 33, 43, 39), (43, 39, 53, 33), (43, 39, 43, 50),
    ):
        c.line(*points, color=MINT)
    c.text(74, 39, c.messages.info_text("info.title"), 30, bold=True, width=640, fallback="MC-QQ-Auth")
    c.text(855, 39, "运行状态", 20, MUTED, anchor="rm", fallback="Runtime status")


def _account(
    c: _Canvas, status: RuntimeStatus, qq_id: str, nickname: str | None, avatar: Image.Image | None,
) -> None:
    c.box(24, 76, 370, 186)
    c.dot(79, 131, 36, "#395563")
    if avatar is None:
        c.dot(79, 131, 34, "#27444a")
        c.line(79, 113, 79, 120, MINT, 2)
        c.dot(79, 112, 3, MINT)
        c.box(59, 120, 99, 148, 8, "#173239", "#62bca9")
        c.dot(71, 132, 2.7, MINT)
        c.dot(87, 132, 2.7, MINT)
        c.line(75, 140, 83, 140, "#90cabe")
    else:
        size = 68 * SCALE
        photo = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size))
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        c.image.paste(photo, (45 * SCALE, 97 * SCALE), mask)
    c.dot(105, 157, 10, CARD)
    c.light(105, 157, _binary_state(status.connected))
    c.text(131, 119, nickname or "QQ 机器人", 25, bold=True, width=218, fallback="QQ Bot")
    c.text(131, 151, f"QQ  {qq_id or EMPTY}", 19 if len(qq_id) <= 15 else 14, MUTED, width=218)


def _services(c: _Canvas, status: RuntimeStatus) -> None:
    c.box(382, 76, 856, 186)
    c.text(402, 104, "服务状态", 20, bold=True, fallback="Services")
    for x, key, state, fallback in (
        (402, "onebot", _binary_state(status.connected), "OneBot"),
        (548, "api", _api_state(status.api), "FastAPI"),
        (694, "database", _binary_state(status.sqlite_readable), "SQLite"),
    ):
        c.box(x, 126, x + 142, 166, 8, "#243644", None)
        c.light(x + 16, 146, state)
        c.text(x + 30, 146, c.label(key), 20, bold=True, width=103, fallback=fallback)


def _resources(c: _Canvas, status: RuntimeStatus) -> None:
    c.box(24, 198, 856, 391)
    c.text(44, 226, "系统资源", 22, bold=True, fallback="System resources")
    system = status.system_name or EMPTY
    os_label = c.label("os")
    if os_label != message_config.DEFAULTS["info.labels.os"]:
        system = f"{os_label} {system}"
    python = status.python_version or EMPTY
    c.text(836, 226, f"{system}   ·   {c.label('python')} {python}", 20, MUTED, anchor="rm",
           width=540, fallback=f"{status.system_name or EMPTY}   ·   Python {python}")
    c.line(44, 253, 836, 253)
    c.line(299, 271, 299, 369)
    c.line(575, 271, 575, 369)

    c.ring(86, 321, status.cpu_percent)
    c.text(148, 304, "CPU", 23, bold=True)
    cores = status.cpu_cores if status.cpu_cores is not None else EMPTY
    c.text(148, 337, f"{cores} {c.label('cpu_cores')}", 19, MUTED, width=133, fallback=f"{cores} cores")
    c.ring(362, 321, status.memory.percent if status.memory is not None else None)
    c.text(424, 304, c.label("memory"), 22, bold=True, width=133, fallback="Memory")
    c.text(424, 337, _usage(status.memory), 19, MUTED, width=141)

    disk_percent = status.disk.percent if status.disk is not None else None
    c.text(597, 287, c.label("disk"), 22, bold=True, width=152, fallback="Disk")
    c.text(836, 287, _percent(disk_percent), 27, bold=True, anchor="rm")
    c.text(597, 324, _usage(status.disk), 19, MUTED, width=239)
    c.box(597, 349, 836, 357, 4, TRACK, None)
    if disk_percent is not None and disk_percent > 0:
        filled = 239 * disk_percent / 100
        c.box(597, 349, 597 + filled, 357, min(4, filled / 2), _resource_color(disk_percent), None)


def _statistics(c: _Canvas, status: RuntimeStatus) -> None:
    c.box(24, 403, 236, 524, 17, "#1a2b36", "#35514f")
    count = str(status.active_bindings) if status.active_bindings is not None else EMPTY
    c.text(130, 449, count, 53, MINT, bold=True, anchor="mm", width=174)
    c.text(130, 494, c.label("bindings"), 20, MUTED, anchor="mm", width=174, fallback="Active bindings")
    c.box(248, 403, 856, 524)
    c.text(269, 429, "运行时长", 21, bold=True, fallback="Uptime")
    c.line(436, 455, 436, 507)
    c.line(632, 455, 632, 507)
    for x, width, key, seconds, fallback in (
        (269, 155, "system_uptime", status.system_seconds, "System"),
        (456, 155, "bot_uptime", status.bot_seconds, "Bot process"),
        (652, 184, "connection_uptime", status.connection_seconds, "OneBot connection"),
    ):
        c.text(x, 467, _duration(seconds, c.cjk), 25, bold=True, width=width)
        c.text(x, 500, c.label(key), 18, MUTED, width=width, fallback=fallback)


def render_status(
    status: RuntimeStatus, *, qq_id: str = "", nickname: str | None = None, avatar: Image.Image | None = None,
) -> bytes:
    c = _Canvas()
    _header(c)
    _account(c, status, qq_id, nickname, avatar)
    _services(c, status)
    _resources(c, status)
    _statistics(c, status)

    local_time = status.checked_at.astimezone()
    offset = local_time.strftime("%z")
    c.line(24, 540, 856, 540)
    c.text(856, 558, f"{local_time:%Y-%m-%d  %H:%M} UTC{offset[:3]}:{offset[3:5]}", 17, MUTED, anchor="rm")
    with BytesIO() as png:
        c.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).save(png, format="PNG")
        return png.getvalue()
