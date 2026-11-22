"""Draw the Aksara Voice app icon (neon sound bars on a deep-space tile) -> tools/aksara.ico.

    .venv\\Scripts\\python.exe tools\\make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "aksara.ico"
S = 512  # draw large, downscale for each icon size


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # tile background: vertical gradient, rounded
    bg = Image.new("RGBA", (S, S))
    px = bg.load()
    for y in range(S):
        c = lerp((22, 44, 104), (4, 8, 26), y / S)
        for x in range(S):
            px[x, y] = (*c, 255)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=255)
    img.paste(bg, (0, 0), mask)

    # glowing edge
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((18, 18, S - 18, S - 18), radius=108, outline=(110, 200, 255, 255), width=8)
    img = Image.alpha_composite(img, edge.filter(ImageFilter.GaussianBlur(6)))
    img = Image.alpha_composite(img, edge)

    # sound bars, cyan -> violet
    heights = [0.28, 0.48, 0.68, 0.86, 0.62, 0.42, 0.24]
    bar_w, gap = 34, 20
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x0 = (S - total) // 2
    bars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(bars)
    for i, h in enumerate(heights):
        hh = int(h * (S - 150))
        x = x0 + i * (bar_w + gap)
        y = (S - hh) // 2
        top = lerp((94, 240, 255), (80, 140, 255), i / 6)
        bottom = lerp((80, 140, 255), (160, 92, 255), i / 6)
        for yy in range(hh):
            d.line((x, y + yy, x + bar_w, y + yy), fill=(*lerp(top, bottom, yy / max(1, hh)), 255))
    bar_mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(bar_mask)
    for i, h in enumerate(heights):
        hh = int(h * (S - 150))
        x = x0 + i * (bar_w + gap)
        y = (S - hh) // 2
        md.rounded_rectangle((x, y, x + bar_w, y + hh), radius=bar_w // 2, fill=255)
    bars.putalpha(bar_mask)
    glow = bars.filter(ImageFilter.GaussianBlur(14))
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, bars)

    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print("saved", OUT)


if __name__ == "__main__":
    main()
