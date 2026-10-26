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


INK = (12, 12, 15)          # 본문 장 바탕
MUTE = (150, 150, 160)      # 보조 글자
RULE = (40, 40, 48)         # 구분선
M = 76                      # 좌우 여백


def frame(img, page, total, show_dots=False):
    """모든 장 공통 틀: 맨 위 진행 표시줄 · 왼쪽 위 로고 · 오른쪽 위 쪽수 · 왼쪽 아래 계정명."""
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 6), fill=(255, 255, 255, 40) if img.mode == "RGBA" else (34, 34, 40))
    d.rectangle((0, 0, int(W * page / max(1, total)), 6), fill=LIME)
    x = M
    for part, col in (("ALT", WHITE), ("+", LIME), ("TAB", WHITE)):
        d.text((x, 62), part, font=ANTON(34), fill=col)
        x += d.textlength(part, font=ANTON(34)) + 2
    d.text((W - M, 64), f"{page:02d} / {total:02d}", font=NM(26), fill=MUTE, anchor="ra")
    d.text((M, H - 64), HANDLE_NOW[0], font=NM(26), fill=MUTE, anchor="ls")
    return d


def kicker(d, x, y, text, fill=None):
    """작은 분류 표시 (빨간 막대 + 글자)."""
    d.rectangle((x, y + 6, x + 6, y + 34), fill=fill or LIME)
    d.text((x + 20, y), text, font=NB(30), fill=fill or LIME)
    return y + 64


def photo_bg(a, strength=0.92):
    """표지 배경: 컬러 사진 + 아래쪽으로 짙어지는 그라데이션 (제목 자리)."""
    im = bg(a, mono=0.1, bright=0.95, contrast=1.05).convert("RGB")
    grad = Image.new("L", (1, H))
    for y in range(H):
        t = max(0.0, (y - H * 0.30) / (H * 0.55))
        top = max(0.0, (H * 0.16 - y) / (H * 0.16)) * 0.55
        grad.putpixel((0, y), int(255 * min(0.97, top + strength * t ** 1.1)))
    return Image.composite(Image.new("RGB", (W, H), INK), im, grad.resize((W, H)))


def paper():
    return Image.new("RGB", (W, H), INK)


HANDLE_NOW = [HANDLE]


def text_bg(a):
    """본문 장 배경: 단색 (글이 잘 읽히고 모든 게시물이 같은 톤)."""
    return paper()


def safe(t):
    return t.replace("…", "...").replace("·", ", ").replace("ㆍ", ", ")  # 블랙한산스에 없는 글자


def cover_text(img, kick, title, sub=None, page=1, total=1):
    """표지 글자 배치 (모든 표지 공통): 분류 → 제목 → 한 줄 부제, 아래쪽 정렬."""
    d = frame(img, page, total)
    tf, lines = fit(d, safe(title), BH, 100, W - 2 * M, 4, 64)
    lh = int(tf.size * 1.18)
    sub_l = []
    if sub:
        sf, sub_l = fit(d, sub, NM, 34, W - 2 * M, 2, 28)
    y = H - 150 - len(lines) * lh - (len(sub_l) * 50 + 24 if sub_l else 0)
    # 글자 뒤는 제목 길이에 맞춰 충분히 어둡게 (밝은 사진에서도 읽히게)
    top = max(0, y - 74 - 260)
    mask = Image.new("L", (1, H))
    for yy in range(H):
        k = 0.0 if yy < top else min(1.0, (yy - top) / 300)
        mask.putpixel((0, yy), int(235 * k))
    img.paste(Image.new("RGB", (W, H), INK), (0, 0), mask.resize((W, H)))
    d = frame(img, page, total)
    kicker(d, M, y - 74, kick)
    for ln in lines:
        d.text((M, y), ln, font=tf, fill=WHITE)
        y += lh
    if sub_l:
        y += 24
        for ln in sub_l:
            d.text((M, y), ln, font=sf, fill=(205, 205, 212))
            y += 50
    return img


def slide_title(a, total):
    label = f"{a.get('category') or '뉴스'} · {a.get('trust')}" if LANG == "ko" else \
        f"{CAT_EN.get(a.get('category'), 'NEWS')} · {TRUST_EN.get(a.get('trust'), 'NEWS')}"
    return cover_text(photo_bg(a), label, a["title"], None, 1, total)


def slide_heading(d, y, label):
    return kicker(d, M, y, label) + 10


def slide_summary(a, total, page=2):
    img = text_bg(a)
    d = frame(img, page, total)
    y = slide_heading(d, 190, L[LANG]["key"])
    from ig_post import to_card_tone
    pts = a.get("tldr", [])[:3] if LANG == "en" else [to_card_tone(x) for x in a.get("tldr", [])[:3]]
    for i, t in enumerate(pts, 1):
        if i > 1:
            d.line([(M, y - 34), (W - M, y - 34)], fill=RULE, width=2)
        d.text((M, y - 4), f"{i:02d}", font=ANTON(54), fill=LIME)
        pf, ls = fit(d, t, NM, 44, W - M - 170, 5)  # 정보 전달 우선: 줄이지 말고 줄 수를 늘린다
        plh = int(pf.size * 1.45)
        for k, ln in enumerate(ls):
            d.text((M + 94, y + k * plh), ln, font=pf, fill=WHITE)
        y += max(1, len(ls)) * plh + 70
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
    f, lh, bottom = NR(38), 62, H - 150
    top_of = lambda k: 190
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
    y = 190  # '자세히 (1/2)' 머리글 없이 본문만 (쪽수는 오른쪽 위에 이미 있음)
    for ls in paras:
        for ln in ls:
            d.text((M, y), ln, font=NR(38), fill=(226, 226, 232))
            y += 62
        y += 34
    return img


def slide_photo(a, im, credit, page, total):
    """공식 발표 이미지 한 장: 잘리지 않게 원본 비율로 가운데, 뒤는 같은 사진을 흐리게."""
    from cards import cover_fit
    back = cover_fit(im, W, H).crop((0, 0, W, H)).filter(ImageFilter.GaussianBlur(28))
    img = Image.blend(back.convert("RGB"), Image.new("RGB", (W, H), INK), 0.72)
    r = min((W - 2 * M) / im.width, (H - 360) / im.height)
    ph = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    img.paste(ph, ((W - ph.width) // 2, (H - ph.height) // 2))
    d = frame(img, page, total)
    if credit:
        d.text((W - M, H - 64), ("이미지 " if LANG == "ko" else "Image ") + credit, font=NM(24), fill=MUTE, anchor="rs")
    return img


def slide_end(a, total):
    img = text_bg(a)
    d = frame(img, total, total)
    y = slide_heading(d, 190, L[LANG]["src"])
    for s in a.get("sources", [])[:4]:
        if y > H - 420:
            break
        d.text((76, y), (L[LANG]["official"] if s.get("official") else "") + s["name"], font=NB(38), fill=WHITE)
        sf, sl = fit(d, s["title"], NR, 30, W - 152, 2, 22)
        for ln in sl:
            y += int(sf.size * 1.6)
            d.text((76, y), ln, font=sf, fill=(170, 170, 178))
        y += 80
    if a.get("img_src"):
        d.text((M, y), f"{L[LANG]['img']} {a['img_src']}", font=NR(28), fill=MUTE)
    sign_off(d)
    return img


def sign_off(d, line=None):
    """마지막 장 서명 (버튼 대신 차분하게)."""
    d.line([(M, H - 250), (W - M, H - 250)], fill=RULE, width=2)
    d.text((M, H - 200), "ALT+TAB", font=ANTON(44), fill=WHITE)
    d.text((M, H - 140), line or ("PC·콘솔 게임 뉴스를 매일 정리합니다" if LANG == "ko" else "Daily PC & console game news"),
           font=NM(30), fill=MUTE)


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
    slides = [cover_text(photo_bg({**arts[0], "img_hd": best_image(arts[0])}), "주간 TOP" + str(len(arts)),
                         f"이번 주 게임 뉴스 TOP{len(arts)}", "가장 화제였던 소식을 골랐습니다", 1, total)]
    for i, a in enumerate(arts, 1):
        sub = to_card_tone(a["tldr"][0]) if a.get("tldr") else None
        slides.append(cover_text(photo_bg({**a, "img_hd": best_image(a)}), f"No.{i}", a["title"], sub, i + 1, total))
    end = paper()
    d = frame(end, total, total)
    y = slide_heading(d, 190, "이번 주 TOP" + str(len(arts)))
    for i, a in enumerate(arts, 1):
        tf, tl = fit(d, safe(a["title"]), NM, 36, W - 2 * M - 70, 2, 28)
        d.text((M, y), f"{i:02d}", font=ANTON(40), fill=LIME)
        for k, ln in enumerate(tl):
            d.text((M + 70, y + k * 50), ln, font=tf, fill=WHITE)
        y += len(tl) * 50 + 34
    sign_off(d, "매주 일요일, 한 주의 게임 뉴스를 정리합니다")
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
    slides = [cover_text(photo_bg(g2a(games[0])), "주간 출시", f"{title_top} 출시 게임 {len(games)}",
                         "출시일을 한눈에 정리했습니다", 1, total)]
    for i, g in enumerate(games, 1):
        dt = datetime.strptime(g["date"], "%Y-%m-%d")
        slides.append(cover_text(photo_bg(g2a(g)), f"{dt.month}/{dt.day} {wd[dt.weekday()]}요일", g["name"],
                                 " · ".join(g.get("platforms") or []) or None, i + 1, total))
    end = paper()
    d = frame(end, total, total)
    y = slide_heading(d, 190, "이번 주 출시 일정")
    for g in games:
        dt = datetime.strptime(g["date"], "%Y-%m-%d")
        d.text((M, y), f"{dt.month}/{dt.day}", font=ANTON(40), fill=LIME)
        tf, tl = fit(d, safe(g["name"]), NM, 36, W - 2 * M - 130, 1, 26)
        d.text((M + 130, y + 2), tl[0], font=tf, fill=WHITE)
        y += 76
    sign_off(d, "매주 월요일, 그 주의 출시 게임을 정리합니다")
    slides.append(end)
    return slides
