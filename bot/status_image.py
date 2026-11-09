from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from runtime_status import RuntimeStatus


WIDTH = 880
HEIGHT = 976


def render_status(status: RuntimeStatus) -> bytes:
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
    draw.text((36, 58), "MC-QQ-Auth", font=title, fill=text)
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
            draw.text((x + 22, top), label, font=label_font, fill=muted)
            text_width = draw.textlength(value, font=value_font)
            font = value_font if text_width <= 350 else ImageFont.load_default(size=value_size * 350 / text_width)
            draw.text((x + 22, top + 24), value, font=font, fill=text)

    draw.line((36, 930, WIDTH - 36, 930), fill="#314456", width=2)
    draw.text((36, 945), "MC-QQ-Auth", font=label_font, fill=muted, anchor="lt")
    draw.text((WIDTH - 36, 945), f"{status.checked_at:%Y-%m-%d %H:%M:%S UTC}",
              font=label_font, fill=muted, anchor="rt")
    with BytesIO() as png:
        image.save(png, format="PNG")
        return png.getvalue()
