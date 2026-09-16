"""Значок ARTFrance — braille-пламя на лиловой плашке."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

# Тот же глиф, что в сообщении (белые точки на лиловом).
BRAILLE_MARK = """
⡀⠀⠀⠀⠀⠀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡠⠀
⠈⣦⡀⠀⠀⠀⠀⠙⢶⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣤⠖⢂⡞⠀⠀
⠀⣼⡇⠀⠀⠀⠀⠀⠈⣿⣆⠀⠀⢀⡴⠋⠀⠀⣰⣿⠁⠀⢸⣧⠀⠀
⢠⣿⡇⠀⠀⠀⠀⠀⠀⣿⠿⠀⢀⣿⠃⠀⠀⢰⣿⡏⠀⠀⢸⣿⣄⠀
⣪⣷⡆⠀⠀⠀⠀⢀⣜⡛⡟⠀⠸⣟⡆⠀⠀⠘⣛⡳⠀⠀⠈⣿⣿⡄
⢟⣭⣷⡀⠀⠀⢀⣯⣟⡟⠀⠀⠀⠟⣽⡧⠀⢸⣟⠻⡆⠀⠀⢰⣦⣝
⠸⡟⣿⡿⣦⠀⠟⠉⡉⣠⡦⣠⡷⠀⡵⠟⢃⣼⡿⣿⠀⠀⢀⣾⣛⠿
⠀⠀⠻⠇⢿⠀⡆⢾⠃⠛⠡⣉⣠⣤⣤⣶⣿⢹⣿⠈⠀⢀⣼⡿⣿⡆
⠀⠀⠀⠀⢀⣼⣿⣌⠀⢰⢤⣄⡉⠛⠉⣚⠋⢈⣁⣠⣴⡟⣿⣧⠸⠃
⠀⠀⠀⠀⢸⡿⠿⠻⠷⠄⢱⣄⠈⠻⠿⠿⠟⣻⣿⠏⣿⡇⢸⠏⠀⠀
⠀⠀⠀⠀⣸⡷⠀⠀⣠⣴⣧⠙⣷⣤⡀⠚⠿⠿⠋⠰⠛⠁⠀⠀⠀⠀
⠀⠀⢀⣾⣿⣿⣾⣿⣿⣿⡿⠀⠈⢿⣿⡆⠀⠱⣶⣾⣷⡀⠀⠀⠀⠀
⠀⠀⠈⢹⣿⣿⣿⣿⡿⠋⠁⠀⡀⠈⠻⣿⣦⠀⠈⠻⣿⣿⣦⡀⠀⠀
⠀⠀⠀⠠⣭⣿⣿⣿⣴⣶⣶⣿⠁⠀⠀⠘⢿⣧⠀⠀⠙⣿⣿⠻⣦⠀
⠀⠀⠀⠀⣾⣿⣿⣿⠿⠟⠛⠁⠀⠀⠀⠀⠘⣿⡄⠀⠀⠹⣿⣧⠈⢣
⠀⠀⠀⠀⠈⠉⠁⠀⢰⣄⠀⠀⠀⢢⠀⠀⠀⢸⡇⠈⡀⠀⢿⣿⡆⠀
⠀⠀⠀⠀⠀⠀⠀⣠⣿⣿⣷⡀⠀⠈⡇⠀⠀⢸⠇⢰⡇⠀⣼⣿⡇⠀
⠀⠀⠀⠀⠀⢠⣾⠟⡛⠛⠛⢇⠀⣸⡗⠀⠀⠘⢀⣾⠇⢀⣿⣿⠃⠀
⠀⠀⠀⠀⠀⡿⠁⣼⡀⠀⠀⢀⣴⣿⠃⠀⠀⢁⣾⡿⢀⣾⣿⠏⠀⠀
⠀⠀⠀⠀⠀⠇⠀⠈⠻⢶⡾⠿⠛⠁⠀⣀⣴⣿⣿⣶⣿⣿⠋⠀⠀⠀
⠀⠀⠀⣠⣶⡿⣿⣿⣶⣦⣴⣦⣶⣶⣿⣿⣿⣿⣿⠿⠋⠀⠀⠀⠀⠀
⠀⠀⠀⣿⠀⠀⠀⠀⠈⠙⠛⠻⠟⠿⠻⠛⠛⠉⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠈
""".strip("\n")

PURPLE = "#C4B5E8"
DOT = "#F7F4FF"

# Bit → (col, row) inside a 2×4 Braille cell
_DOTS = {
    0: (0, 0),
    1: (0, 1),
    2: (0, 2),
    3: (1, 0),
    4: (1, 1),
    5: (1, 2),
    6: (0, 3),
    7: (1, 3),
}


def _cells() -> list[str]:
    return [ln.rstrip(" ") for ln in BRAILLE_MARK.split("\n")]


def render_mark(
    *,
    width: int = 132,
    height: int = 168,
    bg: str = PURPLE,
    fg: str = DOT,
    padding: int = 14,
) -> Image.Image:
    lines = _cells()
    rows = len(lines)
    cols = max(len(ln) for ln in lines) if lines else 1
    cell_w, cell_h = 7, 12
    raw_w = cols * cell_w + padding * 2
    raw_h = rows * cell_h + padding * 2
    img = Image.new("RGBA", (raw_w, raw_h), bg)
    draw = ImageDraw.Draw(img)
    r = 1.35
    for y, line in enumerate(lines):
        for x, ch in enumerate(line):
            code = ord(ch)
            if not (0x2800 <= code <= 0x28FF):
                continue
            bits = code - 0x2800
            ox = padding + x * cell_w
            oy = padding + y * cell_h
            for bit, (dx, dy) in _DOTS.items():
                if bits & (1 << bit):
                    cx = ox + 1.6 + dx * 3.2
                    cy = oy + 1.4 + dy * 2.6
                    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fg)
    if (width, height) != img.size:
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img


def mark_png_bytes(**kwargs) -> bytes:
    buf = BytesIO()
    render_mark(**kwargs).save(buf, format="PNG")
    return buf.getvalue()
