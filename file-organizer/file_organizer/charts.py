"""Anti-aliased charts drawn with Pillow (4x supersampled) so the desktop UI needs no plotting library."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

PALETTE = ["#6ee7d8", "#7c8cff", "#f472b6", "#fbbf24", "#34d399", "#60a5fa", "#fb923c", "#a78bfa", "#94a3b8"]
TRACK = "#243049"
MUTED = "#8a96b2"
TEXT = "#e8ecf6"
SS = 4          # supersampling while drawing
OUT = 2         # returned images are OUT x the logical size, so they stay sharp on scaled displays


def _font(px: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in (("segoeuib.ttf", "DejaVuSans-Bold.ttf") if bold else ("segoeui.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, px * SS)
        except OSError:
            continue
    return ImageFont.load_default()


def _canvas(w: int, h: int, bg: str | None):
    img = Image.new("RGBA", (w * SS, h * SS), bg or (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img: Image.Image, w: int, h: int) -> Image.Image:
    return img.resize((w * OUT, h * OUT), Image.LANCZOS)


def _centered(d: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill: str) -> None:
    d.text((cx * SS, cy * SS), text, font=font, fill=fill, anchor="mm")


def donut(slices: list[tuple[str, float]], size: int = 190, thickness: int = 26, center: str = "",
          caption: str = "", bg: str | None = None) -> Image.Image:
    """slices: [(label, value)]. Colors follow PALETTE order."""
    img, d = _canvas(size, size, bg)
    pad = 4
    box = [pad * SS, pad * SS, (size - pad) * SS, (size - pad) * SS]
    d.ellipse(box, outline=TRACK, width=thickness * SS)
    total = sum(v for _, v in slices) or 1
    start, gap = -90.0, 1.2 if len(slices) > 1 else 0
    for i, (_, v) in enumerate(slices):
        sweep = v / total * 360
        if sweep > gap:
            d.arc(box, start + gap / 2, start + sweep - gap / 2, fill=PALETTE[i % len(PALETTE)],
                  width=thickness * SS)
        start += sweep
    c = size / 2
    if center:
        _centered(d, c, c - (6 if caption else 0), center, _font(int(size * 0.14), True), TEXT)
    if caption:
        _centered(d, c, c + size * 0.12, caption, _font(11), MUTED)
    return _finish(img, size, size)


def ring(fraction: float, size: int = 150, thickness: int = 12, label: str = "", sub: str = "",
         bg: str | None = None) -> Image.Image:
    """Circular progress indicator with a gradient-ish tail."""
    img, d = _canvas(size, size, bg)
    pad = thickness // 2 + 3
    box = [pad * SS, pad * SS, (size - pad) * SS, (size - pad) * SS]
    d.ellipse(box, outline=TRACK, width=thickness * SS)
    f = max(0.0, min(1.0, fraction))
    if f > 0:
        steps = max(2, int(f * 90))
        for i in range(steps):
            t = i / max(1, steps - 1)
            col = tuple(int(a + (b - a) * t) for a, b in zip((124, 140, 255), (110, 231, 216)))
            a0 = -90 + 360 * f * i / steps
            a1 = -90 + 360 * f * (i + 1) / steps + 0.6
            d.arc(box, a0, a1, fill=col, width=thickness * SS)
    c = size / 2
    _centered(d, c, c - (7 if sub else 0), label, _font(int(size * 0.17), True), TEXT)
    if sub:
        _centered(d, c, c + size * 0.14, sub, _font(11), MUTED)
    return _finish(img, size, size)


def _gradient_bar(img: Image.Image, x0: float, y0: float, x1: float, y1: float) -> None:
    """Rounded bar filled with a left-to-right purple to mint gradient."""
    w, h = int((x1 - x0) * SS), int((y1 - y0) * SS)
    strip = Image.new("RGBA", (w, h))
    sd = ImageDraw.Draw(strip)
    c0, c1 = (124, 140, 255), (110, 231, 216)
    for x in range(w):
        t = x / max(1, w - 1)
        sd.line([(x, 0), (x, h)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c0, c1)) + (255,))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], h // 2, fill=255)
    img.paste(strip, (int(x0 * SS), int(y0 * SS)), mask)


def hbars(items: list[tuple[str, float, str]], width: int = 460, row: int = 34, label_w: int = 150,
          bg: str | None = None) -> Image.Image:
    """items: [(label, value, value_text)]. One gradient-free rounded bar per row."""
    h = max(row, row * len(items))
    img, d = _canvas(width, h, bg)
    mx = max((v for _, v, _ in items), default=1) or 1
    f_l, f_v = _font(12), _font(12)
    bar_x0, bar_x1 = label_w, width - 64
    for i, (label, v, vt) in enumerate(items):
        y = i * row + row // 2
        text = label if len(label) <= 22 else label[:21] + "…"
        d.text((0, y * SS), text, font=f_l, fill=TEXT, anchor="lm")
        d.rounded_rectangle([bar_x0 * SS, (y - 4) * SS, bar_x1 * SS, (y + 4) * SS], 4 * SS, fill=TRACK)
        end = bar_x0 + (bar_x1 - bar_x0) * v / mx
        if end > bar_x0 + 2:
            _gradient_bar(img, bar_x0, y - 4, end, y + 4)
        d.text((width * SS, y * SS), vt, font=f_v, fill=MUTED, anchor="rm")
    return _finish(img, width, h)


def timeline(months: list[dict], width: int = 700, height: int = 190, bg: str | None = None) -> Image.Image:
    """months: [{month:'2025-06', photos:n, videos:n}] drawn as stacked columns."""
    img, d = _canvas(width, height, bg)
    f = _font(10)
    if not months:
        _centered(d, width / 2, height / 2, "No dated photos or videos", _font(12), MUTED)
        return _finish(img, width, height)
    left, bottom, top = 8, height - 22, 10
    n = len(months)
    slot = (width - left * 2) / n
    bw = max(2, min(34, slot * 0.72))
    mx = max(m["photos"] + m["videos"] for m in months) or 1
    for i, m in enumerate(months):
        x = left + i * slot + (slot - bw) / 2
        h1 = (bottom - top) * m["photos"] / mx
        h2 = (bottom - top) * m["videos"] / mx
        if h1 >= 1:
            d.rounded_rectangle([x * SS, (bottom - h1) * SS, (x + bw) * SS, bottom * SS], min(3, bw / 2) * SS,
                                fill=PALETTE[1])
        if h2 >= 1:
            d.rounded_rectangle([x * SS, (bottom - h1 - h2) * SS, (x + bw) * SS, (bottom - h1) * SS],
                                min(3, bw / 2) * SS, fill=PALETTE[0])
    every = max(1, math.ceil(n / 9))
    for i in range(0, n, every):
        d.text(((left + i * slot + slot / 2) * SS, (height - 8) * SS), months[i]["month"], font=f, fill=MUTED,
               anchor="mm")
    return _finish(img, width, height)
