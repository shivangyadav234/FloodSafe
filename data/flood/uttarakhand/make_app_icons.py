"""
Draws the home-screen icons in static/icons/ (see WEB APP MANIFEST in
server.py): the landing page's flag mark in white over two flood waves,
on the site's navy.

    python make_app_icons.py

Needs Pillow, which the server itself does not; the PNGs are committed.
"""

import math
import os

from PIL import Image, ImageDraw


NAVY = (11, 53, 88, 255)
WHITE = (255, 255, 255, 255)
WAVE = (127, 179, 213, 255)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons")

# Drawn large and scaled down, which is Pillow's only antialiasing.
CANVAS = 1024


def _wave(draw, y, amplitude, width, left, right):
    points = []
    for i in range(0, 201):
        x = left + (right - left) * i / 200
        points.append((x, y + amplitude * math.sin(2 * math.pi * 2 * i / 200)))
    draw.line(points, fill=WAVE, width=width, joint="curve")


def _draw_mark(draw, scale):
    """The mark, centred, with `scale` = 1 filling about 70% of the canvas."""
    c = CANVAS / 2

    def s(v):
        return c + (v - c) * scale

    # Pole and flag.
    pole_x, top, bottom = s(372), s(208), s(640)
    draw.rounded_rectangle([pole_x - 20 * scale, top, pole_x + 20 * scale, bottom],
                           radius=20 * scale, fill=WHITE)
    flag = []
    for i in range(0, 101):
        t = i / 100
        flag.append((s(392 + 300 * t), s(232 + 26 * math.sin(2 * math.pi * t))))
    for i in range(100, -1, -1):
        t = i / 100
        flag.append((s(392 + 300 * t), s(452 + 26 * math.sin(2 * math.pi * t))))
    draw.polygon(flag, fill=WHITE)

    # Water.
    _wave(draw, s(718), 22 * scale, int(44 * scale), s(236), s(788))
    _wave(draw, s(826), 22 * scale, int(44 * scale), s(236), s(788))


def _icon(size, rounded, scale):
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if rounded:
        draw.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=CANVAS * 0.18, fill=NAVY)
    else:
        draw.rectangle([0, 0, CANVAS, CANVAS], fill=NAVY)
    _draw_mark(draw, scale)
    return image.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # "any": a rounded tile, for browser tabs and launchers that don't mask.
    for size in (192, 512):
        _icon(size, rounded=True, scale=1.0).save(os.path.join(OUT_DIR, f"icon-{size}.png"), optimize=True)

    # "maskable": full-bleed, with the mark inside the central 80% circle
    # that Android's masks are guaranteed to keep.
    _icon(512, rounded=False, scale=0.8).save(os.path.join(OUT_DIR, "icon-maskable-512.png"), optimize=True)

    # iOS rounds the corners itself and shows transparency as black.
    _icon(180, rounded=False, scale=0.9).convert("RGB").save(
        os.path.join(OUT_DIR, "apple-touch-icon.png"), optimize=True)

    print("Icons written to", OUT_DIR)


if __name__ == "__main__":
    main()
