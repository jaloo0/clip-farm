"""Word-by-word captions, the hook title and the credit tag, drawn with Pillow (no special ffmpeg build needed)."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import os

_ROOT = Path(__file__).resolve().parents[1]
FONT_CANDIDATES = [os.environ.get("CF_FONT", "")] + [str(p) for p in sorted((_ROOT / "fonts").glob("*.ttf"))] + [
    "/usr/share/fonts/truetype/montserrat/Montserrat-Black.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
]
CAP_W, CAP_H = 1080, 260
YELLOW = (255, 214, 10, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def clean_word(w: str) -> str:
    w = re.sub(r"[\"“”…]", "", w)
    w = re.sub(r"[.,;:]+$", "", w)
    return w.upper()


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 16) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        txt = clean_word(w["w"])
        if not txt:
            continue
        ww = dict(w, t=txt)
        if cur:
            gap = w["s"] - cur[-1]["e"]
            chars = sum(len(x["t"]) for x in cur) + len(txt) + len(cur)
            if len(cur) >= max_words or chars > max_chars or gap > 0.4 or re.search(r"[.!?,]$", cur[-1]["w"]):
                groups.append(cur)
                cur = []
        cur.append(ww)
    if cur:
        groups.append(cur)
    return groups


def _text_layer(parts: list[tuple[str, tuple]], size: int) -> Image.Image:
    f = font(size)
    space = f.getlength(" ")
    widths = [f.getlength(t) for t, _ in parts]
    total = sum(widths) + space * (len(parts) - 1)
    img = Image.new("RGBA", (CAP_W, CAP_H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (CAP_W, CAP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ds = ImageDraw.Draw(shadow)
    x = (CAP_W - total) / 2
    asc, desc = f.getmetrics()
    y = (CAP_H - (asc + desc)) / 2
    stroke = max(6, size // 10)
    for (t, col), w in zip(parts, widths):
        ds.text((x + 4, y + 7), t, font=f, fill=(0, 0, 0, 150), stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        d.text((x, y), t, font=f, fill=col, stroke_width=stroke, stroke_fill=BLACK)
        x += w + space
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    return Image.alpha_composite(shadow, img)


def fit_size(texts: list[str], start: int = 100, max_w: int = 990) -> int:
    size = start
    while size > 44:
        f = font(size)
        if f.getlength(" ".join(texts)) + size * 0.25 <= max_w:
            return size
        size -= 4
    return size


def render_captions(words: list[dict], duration: float, folder: Path) -> Path:
    """PNG for every caption state + an ffconcat list timed to the words. Returns the list's path."""
    folder.mkdir(parents=True, exist_ok=True)
    for f in folder.glob("cap_*.png"):
        f.unlink()
    blank = folder / "cap_blank.png"
    Image.new("RGBA", (CAP_W, CAP_H), (0, 0, 0, 0)).save(blank)
    groups = group_words(words)
    events: list[tuple[float, float, Path]] = []
    k = 0
    for gi, g in enumerate(groups):
        size = fit_size([w["t"] for w in g])
        g_end = g[-1]["e"] + 0.25
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0]["s"])
        for wi, w in enumerate(g):
            parts = [(x["t"], YELLOW if j == wi else WHITE) for j, x in enumerate(g)]
            p = folder / f"cap_{k:04d}.png"
            _text_layer(parts, size).save(p, optimize=False, compress_level=1)
            k += 1
            s = g[0]["s"] if wi == 0 else w["s"]
            e = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
            events.append((max(0.0, s), max(s + 0.04, e), p))
    lines = ["ffconcat version 1.0"]
    t = 0.0
    for s, e, p in events:
        if s > t + 0.02:
            lines += [f"file '{blank.name}'", f"duration {s - t:.3f}"]
            t = s
        e = min(e, duration)
        if e - t < 0.02:
            continue
        lines += [f"file '{p.name}'", f"duration {e - t:.3f}"]
        t = e
    if t < duration:
        lines += [f"file '{blank.name}'", f"duration {duration - t + 0.5:.3f}"]
    lines += [f"file '{blank.name}'"]
    lst = folder / "captions.ffconcat"
    lst.write_text("\n".join(lines) + "\n")
    return lst


def hook_card(text: str, path: Path, max_w: int = 960) -> tuple[int, int]:
    """White rounded box, black heavy type: the line that makes people stop scrolling."""
    words = text.strip().split()
    size = 66
    f = font(size)
    lines: list[str] = []
    while True:
        lines, cur = [], ""
        for w in words:
            tryline = (cur + " " + w).strip()
            if f.getlength(tryline) <= max_w - 80:
                cur = tryline
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) <= 3 or size <= 44:
            break
        size -= 4
        f = font(size)
    asc, desc = f.getmetrics()
    lh = int((asc + desc) * 1.08)
    w = int(max(f.getlength(l) for l in lines)) + 80
    h = lh * len(lines) + 56
    img = Image.new("RGBA", (w + 24, h + 24), (0, 0, 0, 0))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((12, 18, w + 12, h + 18), 28, fill=(0, 0, 0, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(8))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 12, w + 12, h + 12), 28, fill=(255, 255, 255, 255))
    y = 12 + 28
    for l in lines:
        lw = f.getlength(l)
        d.text((12 + (w - lw) / 2, y), l, font=f, fill=(12, 12, 12, 255))
        y += lh
    Image.alpha_composite(sh, img).save(path)
    return img.size


def credit_tag(text: str, path: Path) -> tuple[int, int]:
    f = font(30)
    w = int(f.getlength(text)) + 40
    h = 54
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), 27, fill=(0, 0, 0, 150))
    d.text((20, (h - sum(f.getmetrics())) / 2), text, font=f, fill=(255, 255, 255, 235))
    img.save(path)
    return img.size
