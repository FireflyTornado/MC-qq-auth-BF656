from bisect import bisect_right
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

import message_config
from runtime_status import RuntimeStatus


WIDTH = 880
HEIGHT = 976


@lru_cache(maxsize=8)
def _cjk_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in (
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
        ("msyh.ttc", 0),
    ):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _draw_fitted(
    draw: ImageDraw.ImageDraw, position: tuple[int, int], value: str, font: ImageFont.FreeTypeFont,
    color: str, width: float, *, anchor: str | None = None,
) -> None:
    # Static labels occupy one line. Normalize whitespace before measuring.
    value = " ".join(value.split())
    if not value.isascii():
        font = _cjk_font(font.size)
    if draw.textlength(value, font=font) > width:
        def prefix_width(length):
            return draw.textlength(value[:length] + "…", font=font)

        length = bisect_right(range(len(value)), width, key=prefix_width) - 1
        value = value[:max(0, length)] + "…"
    draw.text(position, value, font=font, fill=color, anchor=anchor)


def render_status(status: RuntimeStatus) -> bytes:
    messages = message_config.MESSAGES
    image = Image.new("RGB", (WIDTH, HEIGHT), "#101a25")
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=54)
    heading = ImageFont.load_default(size=29)
    value_size = 25
    value_font = ImageFont.load_default(size=value_size)
    label_font = ImageFont.load_default(size=20)
    accent, text, muted = "#75e3c0", "#f2f7fa", "#b5c7d7"

    draw.rectangle((0, 0, WIDTH, 7), fill=accent)
    draw.text((36, 31), "RUNTIME STATUS", font=label_font, fill=accent)
    _draw_fitted(draw, (36, 65), messages.info_text("info.title"), title, text, WIDTH - 72, anchor="lt")
    draw.text((36, 123), "Bot and authorization services", font=label_font, fill=muted)

    for index, (section, rows) in enumerate(status.sections):
        x = 36 + (index % 2) * 414
        y = 170 + (index // 2) * 356
        height = 334 if index < 2 else 380
        draw.rounded_rectangle((x, y, x + 394, y + height), 16, fill="#1a2a38", outline="#314456", width=2)
        draw.text((x + 22, y + 17), section, font=heading, fill=accent)
        draw.line((x + 22, y + 58, x + 372, y + 58), fill="#314456", width=2)
        for row, (label, value) in enumerate(rows):
            top = y + 72 + row * 60
            _draw_fitted(draw, (x + 22, top + 2), messages.info_label(label), label_font, muted, 350, anchor="lt")
            text_width = draw.textlength(value, font=value_font)
            font = value_font if text_width <= 350 else ImageFont.load_default(size=value_size * 350 / text_width)
            draw.text((x + 22, top + 24), value, font=font, fill=text)

    draw.line((36, 930, WIDTH - 36, 930), fill="#314456", width=2)
    timestamp = f"{status.checked_at:%Y-%m-%d %H:%M:%S UTC}"
    footer_width = WIDTH - 72 - 24 - draw.textlength(timestamp, font=label_font)
    _draw_fitted(draw, (36, 945), messages.info_text("info.footer"), label_font, muted,
                 footer_width, anchor="lt")
    draw.text((WIDTH - 36, 945), timestamp, font=label_font, fill=muted, anchor="rt")
    with BytesIO() as png:
        image.save(png, format="PNG")
        return png.getvalue()
