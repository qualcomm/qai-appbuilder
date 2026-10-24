"""Render the brand icon (same mark as frontend/index.html's inline favicon) to icon.ico/png.

Run:  python desktop/src-tauri/icons/render_icon.py     (needs Pillow)
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024            # supersampled canvas; the SVG viewBox is 40x40
K = S / 40.0
OUT = Path(__file__).resolve().parent


def grad(c0, c1, size):
    """Diagonal (0,0)->(1,1) gradient like the SVG's linearGradient."""
    g = Image.new("RGBA", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(c0, c1)) + (255,)
    return g


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def render():
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(base, "RGBA")
    p = hexrgb("#7c6cff")
    d.rounded_rectangle([2 * K, 2 * K, 38 * K, 38 * K], radius=9 * K, fill=hexrgb("#0f1218") + (255,),
                        outline=p + (64,), width=max(1, round(K)))
    for x, y in ((12, 12), (28, 12), (12, 28), (28, 28)):
        d.line([x * K, y * K, 20 * K, 20 * K], fill=p + (102,), width=max(2, round(0.8 * K * 1.4)))
    g = grad(p, hexrgb("#60a5fa"), S)
    mask = Image.new("L", (S, S), 0)
    m = ImageDraw.Draw(mask)
    m.rounded_rectangle([16 * K, 16 * K, 24 * K, 24 * K], radius=1.5 * K, fill=255)
    for x, y in ((12, 12), (28, 12), (12, 28), (28, 28)):
        m.ellipse([(x - 2.5) * K, (y - 2.5) * K, (x + 2.5) * K, (y + 2.5) * K], fill=204)
    base.paste(g, (0, 0), mask)
    return base


if __name__ == "__main__":
    img = render()
    img.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    img.save(OUT / "icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
    print("wrote", OUT / "icon.ico", OUT / "icon.png")
