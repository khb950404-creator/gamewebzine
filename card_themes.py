"""인스타 카드뉴스 테마 3종 (레퍼런스 기반 디자인 시스템).

A NEON     : 흑백 사진 + 형광 라임, 블랙한산스 굵은 제목, 원형 텍스트·점 패턴
B EDITORIAL: 흑백 사진 + 명조 제목 + 손글씨 오렌지 포인트, 위 계정줄·아래 얇은 선
C MONO     : 흑백 사진 + 거대 고스트 영문, 굵은 고딕, 말풍선 요약 카드

  python card_themes.py   # out/themes/ 에 테마별 표지·뉴스 샘플
"""
import json, math, sys
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from cards import W, H, ROOT, fetch_img, cover_fit, wrap

FD = ROOT / "fonts"
WIN = Path("C:/Windows/Fonts")
HANDLE = "@webzinealttab"


def ft(name, size):
    for d in (FD, WIN, Path("/usr/share/fonts")):
        for p in [d / name, *d.glob(f"**/{name}")] if d.exists() else []:
            if p.exists():
                return ImageFont.truetype(str(p), size)
    raise FileNotFoundError(name)


BH = lambda s: ft("BlackHanSans-Regular.ttf", s)
MJ = lambda s: ft("NanumMyeongjo-ExtraBold.ttf", s)
PEN = lambda s: ft("NanumPenScript-Regular.ttf", s)
ANTON = lambda s: ft("Anton-Regular.ttf", s)
NB = lambda s: ft("NotoSansKR-Bold.ttf", s)
NM = lambda s: ft("NotoSansKR-Medium.ttf", s)
NR = lambda s: ft("NotoSansKR-Regular.ttf", s)


def grade(img, mono=0.9, bright=0.62, contrast=1.15):
    """레퍼런스처럼 채도를 빼고 어둡게 눌러 글자를 띄운다."""
    g = ImageOps.grayscale(img).convert("RGB")
    img = Image.blend(img, g, mono)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    return ImageEnhance.Brightness(img).enhance(bright)


def shade(img, top=0.35, strength=235, color=(8, 8, 10)):
    mask = Image.new("L", (1, H))
    for y in range(H):
        t = max(0.0, (y / H - top) / (1 - top))
        mask.putpixel((0, y), int(min(1, t ** 1.2) * strength))
    return Image.composite(Image.new("RGB", img.size, color), img, mask.resize(img.size))


def bg(a, **kw):
    im = fetch_img(a.get("img_hd") or a.get("img") or "") if a.get("img") else None
    if not im:
        im = Image.new("RGB", (W, H), (30, 30, 34))
    return grade(cover_fit(im, W, H), **kw)


def circle_text(d, cx, cy, r, text, fnt, fill):
    n = len(text)
    for i, ch in enumerate(text):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
        layer = Image.new("RGBA", (fnt.size * 2, fnt.size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((fnt.size, fnt.size), ch, font=fnt, fill=fill, anchor="mm")
        layer = layer.rotate(-math.degrees(ang) - 90, resample=Image.BICUBIC)
        d._image.paste(layer, (int(x - fnt.size), int(y - fnt.size)), layer)


def dots(d, x, y, cols, rows, gap, color, r=3):
    for i in range(cols):
        for j in range(rows):
            cx, cy = x + i * gap, y + j * gap
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def lines_of(d, text, fnt, width, maxn):
    ls = wrap(d, text, fnt, width)
    if len(ls) > maxn:
        ls = ls[:maxn]
        ls[-1] = ls[-1].rstrip() + "…"
    return ls


def fit(d, text, font_fn, size, width, maxn, min_size=None):
    """'…'로 자르지 않고, 글자 크기를 줄여 maxn 줄 안에 전부 넣는다 → (폰트, 줄 목록)."""
    from cards import wrap
    min_size = min_size or int(size * 0.6)
    s_ = size
    while True:
        f = font_fn(s_)
        ls = wrap(d, text, f, width)
        if len(ls) <= maxn or s_ <= min_size:
            return f, ls
        s_ -= max(2, size // 20)


def short_title(a, n=26):
    t = a["title"]
    return t if len(t) <= n else t.split(",")[0].split("…")[0]


# ===================== A. NEON =====================
LIME = (200, 242, 60)


def neon_frame(img, page=None):
    d = ImageDraw.Draw(img)
    circle_text(d, 120, 120, 76, "ALT+TAB · GAME NEWS · ", NB(24), LIME)
    d.text((120, 120), "+", font=ANTON(54), fill=LIME, anchor="mm")
    d.text((W - 56, 70), page or "", font=NB(30), fill=LIME, anchor="ra")
    dots(d, W - 56 - 5 * 22, 150, 6, 6, 22, LIME)
    d.text((W - 56, H - 70), HANDLE, font=NB(32), fill=LIME, anchor="ra")
    return d


def neon_news(a, i, n):
    img = shade(bg(a, bright=0.78), 0.30)
    d = neon_frame(img, f"{i}/{n}")
    y = 640
    d.text((96, y), f"{a.get('trust')} · {a.get('category')}", font=NB(32), fill=LIME)
    y += 64
    title = a["title"].replace("…", "...").replace("·", ", ")
    for ln in lines_of(d, title, BH(92), W - 192, 3):
        d.text((96, y), ln, font=BH(92), fill=(255, 255, 255))
        y += 108
    y += 18
    for t in a.get("tldr", [])[:3]:
        ln = lines_of(d, t, NM(34), W - 230, 1)[0]
        d.rectangle((96, y + 18, 112, y + 34), fill=LIME)
        d.text((136, y - 4), ln, font=NM(34), fill=(225, 225, 225))
        y += 62
    return img


def neon_cover(arts):
    img = shade(bg(arts[0], bright=0.78), 0.25)
    d = neon_frame(img, datetime.now().strftime("%m.%d"))
    d.text((96, 560), "오늘의", font=BH(130), fill=LIME)
    d.text((96, 700), "게임 소식", font=BH(130), fill=(255, 255, 255))
    d.rectangle((96, 880, 96 + 500, 940), fill=LIME)
    d.text((112, 884), f"핵심 뉴스 {len(arts)}건 · 넘겨보기 →", font=NB(34), fill=(15, 15, 15))
    d.text((96, 975), "업무 중 잠깐, 3줄 요약", font=NM(34), fill=(220, 220, 220))
    return img


# ===================== B. EDITORIAL =====================
ORANGE = (232, 131, 58)


def edit_frame(img):
    d = ImageDraw.Draw(img)
    d.text((56, 52), HANDLE, font=NM(28), fill=(235, 235, 235))
    d.text((W - 56, 52), "GAME NEWS", font=NB(28), fill=(235, 235, 235), anchor="ra")
    d.line([(56, H - 96), (W - 260, H - 96)], fill=(235, 235, 235), width=2)
    d.text((W - 56, H - 112), "팔로우 →", font=NB(30), fill=(255, 255, 255), anchor="ra")
    return d


def arrow(d, x, y, color):
    d.arc((x, y, x + 90, y + 110), 200, 300, fill=color, width=4)
    d.line([(x + 60, y + 102), (x + 74, y + 92), (x + 76, y + 110)], fill=color, width=4)


def edit_news(a, i, n):
    img = shade(bg(a, mono=1.0), 0.40, 225)
    d = edit_frame(img)
    word = {"공식": "공식 발표", "확인": "업계 확인", "보도": "현지 보도", "루머": "아직 루머"}.get(a.get("trust"), "")
    d.text((W - 300, 560), word, font=PEN(64), fill=ORANGE)
    d.arc((W - 380, 600, W - 250, 720), 110, 200, fill=ORANGE, width=4)
    d.line([(W - 372, 676), (W - 368, 700), (W - 350, 690)], fill=ORANGE, width=4)
    y = 720
    for ln in lines_of(d, a["title"], MJ(84), W - 112, 3):
        d.text((56, y), ln, font=MJ(84), fill=(255, 255, 255))
        y += 104
    y += 20
    for t in a.get("tldr", [])[:2]:
        for ln in lines_of(d, t, NR(32), W - 112, 1):
            d.text((56, y), ln, font=NR(32), fill=(215, 215, 215))
            y += 50
    d.text((56, H - 160), f"{i:02d} / {n:02d}", font=NM(26), fill=(180, 180, 180))
    return img


def edit_cover(arts):
    img = shade(bg(arts[0], mono=1.0), 0.30, 230)
    d = edit_frame(img)
    d.text((56, 560), datetime.now().strftime("%Y. %m. %d"), font=NM(34), fill=(220, 220, 220))
    d.text((56, 620), "오늘의", font=MJ(150), fill=(255, 255, 255))
    d.text((56, 790), "게임 브리핑", font=MJ(150), fill=(255, 255, 255))
    d.text((620, 990), f"핵심 {len(arts)}가지", font=PEN(84), fill=ORANGE)
    arrow(d, 520, 940, ORANGE)
    return img


# ===================== C. MONO =====================
def ghost(img, text, y, size=230, alpha=60):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    words = text.split()
    while len(words) > 1 and ld.textlength(" ".join(words), font=ANTON(120)) > W - 60:
        words = words[:-1]  # 너무 길면 앞 단어만 ("THE WOLF AMONG US REMASTERED" → "THE WOLF AMONG")
    text = " ".join(words)
    f = ANTON(size)
    while ld.textlength(text, font=f) > W - 60 and size > 80:
        size -= 8
        f = ANTON(size)
    ld.text((W // 2, y), text, font=f, fill=(255, 255, 255, alpha), anchor="mt")
    ld.text((W // 2, y + size * 0.95), text, font=f, fill=(0, 0, 0, 0), stroke_width=2,
            stroke_fill=(255, 255, 255, alpha + 40), anchor="mt")
    img.paste(layer, (0, 0), layer)


def mono_frame(img):
    d = ImageDraw.Draw(img)
    d.text((W // 2, H - 70), "A L T + T A B", font=NB(26), fill=(230, 230, 230), anchor="mm")
    return d


def mono_news(a, i, n):
    img = shade(bg(a, mono=1.0, bright=0.7), 0.25, 245)
    en = (a.get("games_en") or [a.get("category") or "NEWS"])[0].split(":")[0].upper()
    ghost(img, en, 60)
    d = mono_frame(img)
    y = 660
    for ln in lines_of(d, a["title"], NB(74), W - 140, 3):
        d.text((W // 2, y), ln, font=NB(74), fill=(255, 255, 255), anchor="mt")
        y += 96
    y += 26
    for t in a.get("tldr", [])[:2]:  # 말풍선 카드
        ln = lines_of(d, t, NM(32), W - 260, 2)
        h = 40 + 48 * len(ln)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle((90, y, W - 90, y + h), 22, fill=(20, 20, 22, 205),
                                               outline=(255, 255, 255, 60), width=2)
        img.paste(layer, (0, 0), layer)
        d = ImageDraw.Draw(img)
        for k, l_ in enumerate(ln):
            d.text((130, y + 20 + k * 48), l_, font=NM(32), fill=(235, 235, 235))
        y += h + 18
    d.text((W - 60, 60), f"{i}/{n}", font=NB(28), fill=(255, 255, 255), anchor="ra")
    return img


def mono_cover(arts):
    img = shade(bg(arts[0], mono=1.0, bright=0.7), 0.2, 245)
    ghost(img, "BRIEFING", 80, 250, 70)
    d = mono_frame(img)
    d.text((W // 2, 640), datetime.now().strftime("%Y.%m.%d"), font=NB(36), fill=(220, 220, 220), anchor="mt")
    d.text((W // 2, 700), "오늘의", font=NB(120), fill=(255, 255, 255), anchor="mt")
    d.text((W // 2, 850), "게임 소식", font=NB(120), fill=(255, 255, 255), anchor="mt")
    d.rounded_rectangle((W // 2 - 230, 1030, W // 2 + 230, 1100), 35, fill=(255, 255, 255))
    d.text((W // 2, 1065), f"핵심 {len(arts)}건 넘겨보기 →", font=NB(32), fill=(10, 10, 10), anchor="mm")
    return img


THEMES = {"A_neon": (neon_cover, neon_news), "B_editorial": (edit_cover, edit_news), "C_mono": (mono_cover, mono_news)}

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = ROOT / "out" / "themes"
    out.mkdir(parents=True, exist_ok=True)
    arts = sorted([a for a in json.loads((ROOT / "data/articles.json").read_text(encoding="utf-8"))
                   if isinstance(a.get("title"), str) and a.get("img")], key=lambda a: -a["ts"])[:6]
    sheets = []
    for name, (cov, news) in THEMES.items():
        imgs = [cov(arts[:5]), news(arts[0], 1, 5), news(arts[2], 3, 5)]
        for k, im in enumerate(imgs):
            im.save(out / f"{name}_{k}.jpg", quality=90)
        sheets.append(imgs)
    # 비교용 한 장: 테마별 한 줄(표지·뉴스·뉴스)
    s = 360
    sheet = Image.new("RGB", (s * 3 + 40, int(s * 1.25) * 3 + 40), (230, 230, 230))
    for r, imgs in enumerate(sheets):
        for c, im in enumerate(imgs):
            sheet.paste(im.resize((s, int(s * 1.25))), (10 + c * (s + 10), 10 + r * (int(s * 1.25) + 10)))
    sheet.save(out / "compare.jpg", quality=88)
    print("완료:", sorted(p.name for p in out.glob("*.jpg")))
