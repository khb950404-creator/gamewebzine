"""기사 1건 → 인스타 캐러셀 (알트탭 레드 테마).

  1장  썸네일: 이미지 + 제목만
  2장  핵심 요약 (3줄)
  3~N  본문 상세 (길면 여러 장)
  끝장  출처 + 팔로우 안내 (사이트 주소는 노출하지 않음)

  python carousel.py [기사ID]   # out/carousel/<ID>/ 에 생성
"""
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

from cards import W, H, ROOT, wrap
from card_themes import BH, NB, NM, NR, ANTON, bg, shade, circle_text, dots, lines_of, HANDLE

WHITE, SOFT = (255, 255, 255), (222, 222, 226)
LIME = (229, 72, 77)  # 사이트와 같은 핑크빛 레드 (#e5484d)


def frame(img, page, total, show_dots=False):
    d = ImageDraw.Draw(img)
    # 디자인 포인트: 로고의 '+' (가늘게 — 굵으면 의료용 십자가처럼 보임)
    cx, cy, L, t = 100, 100, 26, 4
    d.rectangle((cx - L, cy - t, cx + L, cy + t), fill=LIME)
    d.rectangle((cx - t, cy - L, cx + t, cy + L), fill=LIME)
    d.text((W - 56, 70), f"{page}/{total}", font=NB(30), fill=LIME, anchor="ra")
    d.text((W - 56, H - 70), HANDLE, font=NB(32), fill=LIME, anchor="ra")
    return d


def text_bg(a):
    """본문 장 배경: 같은 이미지를 흐리고 아주 어둡게 → 장이 넘어가도 한 세트로 보이게."""
    im = bg(a, bright=0.5).filter(ImageFilter.GaussianBlur(18))
    return Image.blend(im, Image.new("RGB", (W, H), (10, 10, 12)), 0.55)


def safe(t):
    return t.replace("…", "...")  # 블랙한산스에 없는 글자


def slide_title(a, total):
    img = shade(bg(a, bright=0.8), 0.40, 240)
    d = frame(img, 1, total, show_dots=True)
    lines = lines_of(d, safe(a["title"]), BH(108), W - 150, 4)
    y = H - 230 - len(lines) * 126
    d.text((76, y - 70), f"{a.get('trust')} · {a.get('category')}", font=NB(34), fill=LIME)
    for ln in lines:
        d.text((76, y), ln, font=BH(108), fill=WHITE)
        y += 126
    return img


def slide_heading(d, y, label):
    d.rectangle((76, y + 8, 90, y + 44), fill=LIME)
    d.text((108, y), label, font=BH(48), fill=LIME)
    return y + 100


def slide_summary(a, total):
    img = text_bg(a)
    d = frame(img, 2, total)
    y = slide_heading(d, 300, "핵심 요약")
    for i, t in enumerate(a.get("tldr", [])[:3], 1):
        d.text((76, y), f"{i:02d}", font=ANTON(64), fill=LIME)
        ls = lines_of(d, t, NB(46), W - 250, 3)
        for k, ln in enumerate(ls):
            d.text((180, y + 6 + k * 64), ln, font=NB(46), fill=WHITE)
        y += max(1, len(ls)) * 64 + 60
    if a.get("why"):
        y += 10
        d.line([(76, y), (W - 76, y)], fill=(70, 70, 76), width=2)
        y += 30
        for ln in lines_of(d, a["why"], NM(36), W - 152, 2 if y > 1050 else 3):
            d.text((76, y), ln, font=NM(36), fill=SOFT)
            y += 54
    return img


def body_pages(a):
    """본문 문단을 카드 한 장 높이에 맞게 나눈다."""
    probe = ImageDraw.Draw(Image.new("RGB", (W, H)))
    f, lh, top, bottom = NR(38), 62, 400, H - 170
    pages, cur, y = [], [], top
    for para in a.get("body", []):
        ls = wrap(probe, para, f, W - 152)
        need = len(ls) * lh + 34
        if cur and y + need > bottom:
            pages.append(cur)
            cur, y = [], top
        cur.append(ls)
        y += need
    if cur:
        pages.append(cur)
    return pages[:3]  # 최대 3장


def slide_body(a, paras, page, total, idx, n):
    img = text_bg(a)
    d = frame(img, page, total)
    slide_heading(d, 300, "자세히" + (f" ({idx}/{n})" if n > 1 else ""))
    y = 400
    for ls in paras:
        for ln in ls:
            d.text((76, y), ln, font=NR(38), fill=SOFT)
            y += 62
        y += 34
    return img


def slide_end(a, total):
    img = text_bg(a)
    d = frame(img, total, total)
    y = slide_heading(d, 300, "출처")
    for s in a.get("sources", [])[:4]:
        if y > H - 520:  # 아래 버튼과 겹치지 않게
            break
        d.text((76, y), ("[공식] " if s.get("official") else "") + s["name"], font=NB(38), fill=WHITE)
        for ln in lines_of(d, s["title"], NR(30), W - 152, 2):
            y += 48
            d.text((76, y), ln, font=NR(30), fill=(170, 170, 178))
        y += 80
    if a.get("img_src"):
        d.text((76, y), f"이미지: {a['img_src']}", font=NR(28), fill=(150, 150, 158))
    # 행동 유도
    d.rounded_rectangle((76, H - 330, W - 76, H - 170), 24, fill=LIME)
    d.text((W // 2, H - 280), "매일 게임 소식 3줄", font=NB(40), fill=WHITE, anchor="mm")
    d.text((W // 2, H - 220), "팔로우하고 받아보기", font=BH(56), fill=WHITE, anchor="mm")
    return img


def best_image(a):
    """카드 배경은 크게 늘어나므로 고해상도 우선: 스크린샷(1920) > Steam 키아트(3840) > 기존 이미지."""
    hd = a.get("img_hd") or a.get("img") or ""
    if a.get("appid") and ("1920x1080" not in hd):
        from deals_carousel import key_art
        return key_art(a["appid"], hd)
    return hd


def build_carousel(a):
    a = {**a, "img_hd": best_image(a)}
    pages = body_pages(a)
    total = 2 + len(pages) + 1
    slides = [slide_title(a, total), slide_summary(a, total)]
    for k, p in enumerate(pages, 1):
        slides.append(slide_body(a, p, 2 + k, total, k, len(pages)))
    slides.append(slide_end(a, total))
    return slides


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    arts = [a for a in json.loads((ROOT / "data/articles.json").read_text(encoding="utf-8")) if isinstance(a.get("title"), str)]
    if len(sys.argv) > 1:
        targets = [a for a in arts if a["id"] in sys.argv[1:]]
    else:  # 샘플: 이미지 있고 본문 긴 최신 기사 2건
        targets = sorted([a for a in arts if a.get("img") and len(" ".join(a.get("body", []))) > 400],
                         key=lambda a: -a["ts"])[:2]
    for a in targets:
        out = ROOT / "out" / "carousel" / a["id"]
        out.mkdir(parents=True, exist_ok=True)
        slides = build_carousel(a)
        for i, im in enumerate(slides, 1):
            im.save(out / f"{i:02d}.jpg", quality=90)
        s = 300
        sheet = Image.new("RGB", (len(slides) * (s + 10) + 10, int(s * 1.25) + 20), (230, 230, 230))
        for i, im in enumerate(slides):
            sheet.paste(im.resize((s, int(s * 1.25))), (10 + i * (s + 10), 10))
        sheet.save(out / "strip.jpg", quality=88)
        print(a["id"], len(slides), "장 →", out)
