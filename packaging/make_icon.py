"""Draw the app icon: a dive profile, steep descent, bottom time, then deco
stops on the way up. Writes packaging/bottomtime.icns (needs Pillow and
macOS's iconutil). Run it again only if the design changes."""

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent


def draw(size: int) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # Apple's icon grid: an 824 px rounded square centred on a 1024 canvas.
    tile = Image.new("RGBA", (size, size))
    grad = ImageDraw.Draw(tile)
    top, bottom = (42, 120, 214), (13, 54, 107)
    for y in range(size):
        f = y / size
        grad.line([(0, y), (size, y)], fill=tuple(round(a + (b - a) * f) for a, b in zip(top, bottom)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100 * s, 100 * s, 924 * s, 924 * s], radius=185 * s, fill=255)
    img.paste(tile, (0, 0), mask)
    d = ImageDraw.Draw(img)
    profile = [(230, 290), (380, 690), (510, 690), (590, 520), (650, 520), (675, 440), (730, 440), (755, 360), (795, 360), (810, 290)]
    d.line([(x * s, y * s) for x, y in profile], fill=(255, 255, 255, 255), width=max(2, round(44 * s)), joint="curve")
    r = 22 * s
    for x, y in (profile[0], profile[-1]):
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=(255, 255, 255, 255))
    return img


with tempfile.TemporaryDirectory() as tmp:
    iconset = Path(tmp) / "bottomtime.iconset"
    iconset.mkdir()
    for px in (16, 32, 128, 256, 512):
        draw(px).save(iconset / f"icon_{px}x{px}.png")
        draw(px * 2).save(iconset / f"icon_{px}x{px}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "bottomtime.icns")], check=True)
print("wrote packaging/bottomtime.icns")
