"""Generate a neutral demo app icon (no real company branding).

Draws a rounded-square teal badge with a laptop + gear glyph so the
desktop shortcut looks like ordinary software. Writes a1demo.ico with
multi-resolution frames Inno/Windows expect.
"""
from PIL import Image, ImageDraw

SIZE = 256


def _rounded(draw, box, radius, fill):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build(path='a1demo.ico'):
    img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # background rounded square (teal gradient-ish)
    _rounded(d, (8, 8, SIZE - 8, SIZE - 8), 44, (13, 110, 120, 255))
    _rounded(d, (16, 16, SIZE - 16, SIZE - 16), 38, (16, 138, 148, 255))

    cx = SIZE // 2
    # laptop body
    d.rounded_rectangle((60, 96, 196, 168), radius=10, fill=(245, 250, 250, 255))
    d.rectangle((74, 110, 182, 154), fill=(20, 60, 66, 255))  # screen
    d.polygon([(48, 190), (208, 190), (196, 168), (60, 168)],
              fill=(220, 232, 232, 255))  # base
    # gear accent (top-right)
    gx, gy, gr = 182, 92, 30
    d.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=(240, 173, 78, 255))
    d.ellipse((gx - 12, gy - 12, gx + 12, gy + 12), fill=(20, 60, 66, 255))
    for a in range(0, 360, 45):
        import math
        x = gx + (gr + 6) * math.cos(math.radians(a))
        y = gy + (gr + 6) * math.sin(math.radians(a))
        d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(240, 173, 78, 255))

    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print('wrote', path)


if __name__ == '__main__':
    build()
