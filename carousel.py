"""기사 1건 → 인스타 캐러셀 (알트탭 레드 테마).

  1장  썸네일: 이미지 + 제목만
  2장  핵심 요약 (3줄)
  3~N  본문 상세 (길면 여러 장)
  끝장  출처 + 팔로우 안내 (사이트 주소는 노출하지 않음)

  python carousel.py [기사ID]   # out/carousel/<ID>/ 에 생성
"""
import json, sys
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

from cards import W, H, ROOT, wrap
from card_themes import BH, NB, NM, NR, ANTON, bg, shade, circle_text, dots, lines_of, fit, HANDLE

WHITE, SOFT = (255, 255, 255), (222, 222, 226)
LIME = (229, 72, 77)  # 사이트와 같은 핑크빛 레드 (#e5484d)
LANG = "ko"  # build_carousel(lang=...) 이 바꾼다
L = {"ko": {"key": "핵심 요약", "more": "자세히", "src": "출처", "img": "이미지", "cta1": "매일 게임 소식 3줄", "cta2": "팔로우하고 받아보기",
            "official": "[공식] "},
     "en": {"key": "KEY POINTS", "more": "DETAILS", "src": "SOURCES", "img": "Image", "cta1": "Game news in 3 lines",
            "cta2": "Follow for daily updates", "official": "[Official] "}}
TRUST_EN = {"공식": "OFFICIAL", "확인": "CONFIRMED", "보도": "REPORT", "루머": "RUMOR"}
CAT_EN = {"신작": "NEW GAME", "업데이트": "UPDATE", "업계": "INDUSTRY", "하드웨어": "HARDWARE", "할인·이벤트": "DEALS",
          "e스포츠": "ESPORTS", "기타": "NEWS"}


def frame(img, page, total, show_dots=False):
    d = ImageDraw.Draw(img)
    # 디자인 포인트: 로고의 '+' (가늘게 — 굵으면 의료용 십자가처럼 보임)
    cx, cy, L, t = 100, 100, 26, 4
    # 왼쪽 위 '+' 표시는 뺐다 (거슬린다는 피드백)
    d.text((W - 56, 70), f"{page}/{total}", font=NB(30), fill=LIME, anchor="ra")
    d.text((W - 56, H - 70), HANDLE_NOW[0], font=NB(32), fill=LIME, anchor="ra")
    return d


HANDLE_NOW = [HANDLE]


def text_bg(a):
    """본문 장 배경: 같은 이미지를 흐리고 아주 어둡게 → 장이 넘어가도 한 세트로 보이게."""
    im = bg(a, bright=0.5).filter(ImageFilter.GaussianBlur(18))
    return Image.blend(im, Image.new("RGB", (W, H), (10, 10, 12)), 0.55)


def safe(t):
    return t.replace("…", "...").replace("·", ", ").replace("ㆍ", ", ")  # 블랙한산스에 없는 글자


def slide_title(a, total):
    img = shade(bg(a, **({"mono": 0.0, "bright": 1.0} if a.get("esports") else {"bright": 0.8})), 0.40, 240)
    d = frame(img, 1, total, show_dots=True)
    tf, lines = fit(d, safe(a["title"]), BH, 108, W - 150, 4)
    lh = int(tf.size * 1.17)
    y = H - 230 - len(lines) * lh
    label = f"{a.get('trust')} · {a.get('category')}" if LANG == "ko" else         f"{TRUST_EN.get(a.get('trust'), 'NEWS')} · {CAT_EN.get(a.get('category'), 'NEWS')}"
    d.text((76, y - 70), label, font=NB(34), fill=LIME)
    for ln in lines:
        d.text((76, y), ln, font=tf, fill=WHITE)
        y += lh
    return img


def slide_heading(d, y, label):
    d.rectangle((76, y + 8, 90, y + 44), fill=LIME)
    d.text((108, y), label, font=BH(48), fill=LIME)
    return y + 100


def slide_summary(a, total, page=2):
    img = text_bg(a)
    d = frame(img, page, total)
    y = slide_heading(d, 300, L[LANG]["key"])
    from ig_post import to_card_tone
    pts = a.get("tldr", [])[:3] if LANG == "en" else [to_card_tone(x) for x in a.get("tldr", [])[:3]]
    for i, t in enumerate(pts, 1):
        d.text((76, y), f"{i:02d}", font=ANTON(64), fill=LIME)
        pf, ls = fit(d, t, NB, 46, W - 250, 5)  # 정보 전달 우선: 줄이지 말고 줄 수를 늘린다
        plh = int(pf.size * 1.39)
        for k, ln in enumerate(ls):
            d.text((180, y + 6 + k * plh), ln, font=pf, fill=WHITE)
        y += max(1, len(ls)) * plh + 60
    if a.get("why") and y < H - 330:  # 요약이 길어 자리가 없으면 '왜 중요한가'는 생략 (본문·캡션에 있음)
        y += 10
        d.line([(76, y), (W - 76, y)], fill=(70, 70, 76), width=2)
        y += 30
        wf, wl = fit(d, a["why"], NM, 36, W - 152, max(1, min(3, (H - 200 - y) // 54)), 24)
        for ln in wl:
            d.text((76, y), ln, font=wf, fill=SOFT)
            y += int(wf.size * 1.5)
    return img


def gallery_imgs(a):
    """기사에 붙은 공식 발표 이미지 중 실제로 열리는 것만 (본문 장마다 한 장씩)."""
    if "_gal" not in a:
        from cards import fetch_img
        out = []
        for g in a.get("gallery", [])[:6]:
            im = fetch_img(g.get("src", ""))
            if im and im.width >= 500:
                out.append((im, g.get("credit", "")))
        a["_gal"] = out
    return a["_gal"]


def body_pages(a):
    """본문 문단을 카드 한 장 높이에 맞게 나눈다. 공식 이미지가 있는 장은 위쪽에 사진 칸을 둔다."""
    probe = ImageDraw.Draw(Image.new("RGB", (W, H)))
    f, lh, bottom = NR(38), 62, H - 170
    top_of = lambda k: 230
    pages, cur, y = [], [], top_of(0)
    for para in a.get("body", []):
        ls = wrap(probe, para, f, W - 152)
        need = len(ls) * lh + 34
        if cur and y + need > bottom:
            pages.append(cur)
            cur, y = [], top_of(len(pages))
        cur.append(ls)
        y += need
    if cur:
        pages.append(cur)
    return pages[:6]  # 본문 최대 6장 (표지·요약·마무리 포함 10장 이내)


def slide_body(a, paras, page, total, idx, n):
    img = text_bg(a)
    d = frame(img, page, total)
    y = 230  # '자세히 (1/2)' 머리글 없이 본문만 (쪽수는 오른쪽 위에 이미 있음)
    for ls in paras:
        for ln in ls:
            d.text((76, y), ln, font=NR(38), fill=SOFT)
            y += 62
        y += 34
    return img


def slide_photo(a, im, credit, page, total):
    """공식 발표 이미지 한 장: 잘리지 않게 원본 비율로 가운데, 뒤는 같은 사진을 흐리게."""
    from cards import cover_fit
    back = cover_fit(im, W, H).crop((0, 0, W, H)).filter(ImageFilter.GaussianBlur(28))
    img = Image.blend(back.convert("RGB"), Image.new("RGB", (W, H), (10, 10, 12)), 0.6)
    r = min(W / im.width, (H - 360) / im.height)
    ph = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    img.paste(ph, ((W - ph.width) // 2, (H - ph.height) // 2))
    d = frame(img, page, total)
    if credit:
        d.text((76, H - 70), ("이미지: " if LANG == "ko" else "Image: ") + credit, font=NR(28), fill=SOFT)
    return img


def slide_end(a, total):
    img = text_bg(a)
    d = frame(img, total, total)
    y = slide_heading(d, 300, L[LANG]["src"])
    for s in a.get("sources", [])[:4]:
        if y > H - 520:  # 아래 버튼과 겹치지 않게
            break
        d.text((76, y), (L[LANG]["official"] if s.get("official") else "") + s["name"], font=NB(38), fill=WHITE)
        sf, sl = fit(d, s["title"], NR, 30, W - 152, 2, 22)
        for ln in sl:
            y += int(sf.size * 1.6)
            d.text((76, y), ln, font=sf, fill=(170, 170, 178))
        y += 80
    if a.get("img_src"):
        d.text((76, y), f"{L[LANG]['img']}: {a['img_src']}", font=NR(28), fill=(150, 150, 158))
    # 행동 유도
    d.rounded_rectangle((76, H - 330, W - 76, H - 170), 24, fill=LIME)
    d.text((W // 2, H - 280), L[LANG]["cta1"], font=NB(40), fill=WHITE, anchor="mm")
    d.text((W // 2, H - 220), L[LANG]["cta2"], font=BH(56 if LANG == "ko" else 50), fill=WHITE, anchor="mm")
    return img


def best_image(a):
    """카드 배경은 크게 늘어나므로 고해상도 우선: 스크린샷(1920) > Steam 키아트(3840) > 기존 이미지."""
    hd = a.get("img_hd") or a.get("img") or ""
    if a.get("appid") and ("1920x1080" not in hd):
        from deals_carousel import key_art
        return key_art(a["appid"], hd)
    return hd


def build_carousel(a, lang="ko", handle=None):
    global LANG
    LANG = lang
    HANDLE_NOW[0] = handle or HANDLE
    a = {**a, "img_hd": best_image(a)}
    if lang == "en":  # 영문: 제목·핵심 3줄·출처 (본문은 한국어라 생략)
        a = {**a, "title": a.get("title_en") or a["title"], "tldr": a.get("tldr_en") or [], "why": "", "body": []}
    gal = gallery_imgs(a)[:3]
    pages = body_pages(a)[:max(1, 10 - 3 - len(gal))]
    total = 2 + len(gal) + len(pages) + 1
    slides = [slide_title(a, total)]
    for k, (im, cr) in enumerate(gal):
        slides.append(slide_photo(a, im, cr, 2 + k, total))
    slides.append(slide_summary(a, total, page=2 + len(gal)))
    for k, p in enumerate(pages, 1):
        slides.append(slide_body(a, p, 2 + len(gal) + k, total, k, len(pages)))
    slides.append(slide_end(a, total))
    return slides


def build_weekly_top(arts, handle=None):
    """일요일 시리즈: 이번 주 게임 뉴스 TOP5 — 표지 + 기사별 1장(제목·한 줄) + 마무리."""
    global LANG
    LANG = "ko"
    HANDLE_NOW[0] = handle or HANDLE
    from ig_post import to_card_tone
    total = len(arts) + 2
    cover = shade(bg({**arts[0], "img_hd": best_image(arts[0])}, bright=0.7), 0.25, 240)
    d = frame(cover, 1, total)
    d.text((76, 640), "이번 주", font=BH(110), fill=WHITE)
    d.text((76, 770), "게임 뉴스", font=BH(110), fill=WHITE)
    d.text((76 + d.textlength("게임 뉴스 ", font=BH(110)), 910), "TOP" + str(len(arts)), font=ANTON(150), fill=LIME, anchor="ls")
    d.text((76, 960), "가장 화제였던 소식만 골랐습니다", font=NM(36), fill=SOFT)
    slides = [cover]
    for i, a in enumerate(arts, 1):
        a2 = {**a, "img_hd": best_image(a)}
        img = shade(bg(a2, bright=0.8), 0.35, 242)
        d = frame(img, i + 1, total)
        d.text((76, 600), f"{i}", font=ANTON(200), fill=LIME)
        y = 840
        tf2, tl2 = fit(d, safe(a["title"]), BH, 88, W - 152, 3)
        for ln in tl2:
            d.text((76, y), ln, font=tf2, fill=WHITE)
            y += int(tf2.size * 1.18)
        if a.get("tldr"):
            tf3, tl3 = fit(d, to_card_tone(a["tldr"][0]), NM, 38, W - 152, 2, 26)
            for ln in tl3:
                d.text((76, y + 20), ln, font=tf3, fill=SOFT)
                y += int(tf3.size * 1.47)
        slides.append(img)
    end = text_bg(arts[0])
    d = frame(end, total, total)
    d.text((76, 420), "매주 일요일", font=BH(90), fill=WHITE)
    d.text((76, 530), "한 주 정리", font=BH(90), fill=LIME)
    d.rounded_rectangle((76, H - 330, W - 76, H - 170), 24, fill=LIME)
    d.text((W // 2, H - 280), L["ko"]["cta1"], font=NB(40), fill=WHITE, anchor="mm")
    d.text((W // 2, H - 220), L["ko"]["cta2"], font=BH(56), fill=WHITE, anchor="mm")
    slides.append(end)
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


def build_weekly_releases(games, title_top="이번 주", handle=None):
    """월요일 시리즈: 이번 주 출시 게임 — 표지 + 게임별 1장(날짜·이름·플랫폼·장르) + 저장 안내."""
    global LANG
    LANG = "ko"
    HANDLE_NOW[0] = handle or HANDLE
    total = len(games) + 2
    wd = "월화수목금토일"
    def g2a(g):
        return {"img": g.get("img_hd") or g.get("img"), "img_hd": g.get("img_hd") or g.get("img")}
    cover = shade(bg(g2a(games[0]), bright=0.7), 0.25, 240)
    d = frame(cover, 1, total)
    d.text((76, 640), title_top, font=BH(110), fill=WHITE)
    d.text((76, 770), "출시 게임", font=BH(110), fill=WHITE)
    d.text((76 + d.textlength("출시 게임 ", font=BH(110)), 910), str(len(games)), font=ANTON(150), fill=LIME, anchor="ls")
    d.text((76, 960), "저장해두고 하나씩 체크하세요", font=NM(36), fill=SOFT)
    slides = [cover]
    for i, g in enumerate(games, 1):
        img = shade(bg(g2a(g), bright=0.8), 0.35, 242)
        d = frame(img, i + 1, total)
        dt = datetime.strptime(g["date"], "%Y-%m-%d")
        d.text((76, 790), f"{dt.month}/{dt.day}", font=ANTON(150), fill=LIME, anchor="ls")
        d.text((76 + d.textlength(f"{dt.month}/{dt.day} ", font=ANTON(150)), 790), f"{wd[dt.weekday()]}요일",
               font=BH(60), fill=LIME, anchor="ls")
        y = 840
        tf2, tl2 = fit(d, safe(g["name"]), BH, 92, W - 152, 3)
        for ln in tl2:
            d.text((76, y), ln, font=tf2, fill=WHITE)
            y += int(tf2.size * 1.18)
        info = " / ".join(g.get("platforms") or [])
        if info:
            tf3, tl3 = fit(d, info, NM, 38, W - 152, 2, 26)
            for ln in tl3:
                d.text((76, y + 20), ln, font=tf3, fill=SOFT)
                y += int(tf3.size * 1.47)
        slides.append(img)
    end = text_bg(g2a(games[0]))
    d = frame(end, total, total)
    d.text((76, 420), "매주 월요일", font=BH(90), fill=WHITE)
    d.text((76, 530), "출시 게임 정리", font=BH(90), fill=LIME)
    d.rounded_rectangle((76, H - 330, W - 76, H - 170), 24, fill=LIME)
    d.text((W // 2, H - 280), "저장하고 출시일 놓치지 말기", font=NB(40), fill=WHITE, anchor="mm")
    d.text((W // 2, H - 220), L["ko"]["cta2"], font=BH(56), fill=WHITE, anchor="mm")
    slides.append(end)
    return slides
