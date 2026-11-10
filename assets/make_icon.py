"""Draw the Reelm Encoder logo -> src/reelm_encoder/assets/icon.ico + icon.png.

Run (from the reelm-encoder folder):  uv run --with pillow assets/make_icon.py

Same family as the Video Downloader and Subtitle Maker logos (rounded tile,
white glyph) in Reelm's lavender -> mauve: a stack of layers, i.e. one video
turned into several quality levels. Matches the inline SVG in desktop.html.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "reelm_encoder" / "assets"
S = 1024
U = S / 100

TOP_LEFT = (124, 108, 240)  # #7C6CF0 deep lavender
BOTTOM_RIGHT = (185, 120, 159)  # #B9789F deep mauve


def u(*vals: float) -> list[float]:
    return [v * U for v in vals]


def gradient() -> Image.Image:
    img = Image.new("RGB", (S, S))
    px = img.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * (S - 1))
            px[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(TOP_LEFT, BOTTOM_RIGHT, strict=True)
            )
    return img


Point = tuple[float, float]


def rounded_outline(pts: list[Point], r: float, steps: int = 48) -> list[Point]:
    """Outline of a convex polygon grown outward by r (smooth arcs at corners)."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    def normal(a, b):
        nx, ny = b[1] - a[1], a[0] - b[0]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        if nx * (mx - cx) + ny * (my - cy) < 0:
            nx, ny = -nx, -ny
        return math.atan2(ny, nx)

    out = []
    for i in range(len(pts)):
        prev, cur, nxt = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        a1, a2 = normal(prev, cur), normal(cur, nxt)
        span = (a2 - a1) % (2 * math.pi)
        if span > math.pi:
            span -= 2 * math.pi
        for k in range(steps + 1):
            a = a1 + span * k / steps
            out.append((cur[0] + r * math.cos(a), cur[1] + r * math.sin(a)))
    return out


def chevron(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: float, fill) -> None:
    """A thick polyline with round joins and round ends."""
    d.line(pts, fill=fill, width=int(width), joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def draw() -> Image.Image:
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(u(0, 0, 100, 100), radius=24 * U, fill=255)
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(gradient(), (0, 0), mask)

    d = ImageDraw.Draw(tile)
    white = (255, 255, 255, 255)
    top = [(50 * U, 22 * U), (75 * U, 35 * U), (50 * U, 48 * U), (25 * U, 35 * U)]
    d.polygon(rounded_outline(top, 3.5 * U), fill=white)
    chevron(d, [(25 * U, 50 * U), (50 * U, 63 * U), (75 * U, 50 * U)], 7 * U, white)
    chevron(d, [(25 * U, 63.5 * U), (50 * U, 76.5 * U), (75 * U, 63.5 * U)], 7 * U, white)
    return tile


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = draw()
    img.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    img.resize((256, 256), Image.LANCZOS).save(
        OUT / "icon.ico",
        sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)],
    )
    print("wrote", OUT / "icon.png", "and", OUT / "icon.ico")


if __name__ == "__main__":
    main()
