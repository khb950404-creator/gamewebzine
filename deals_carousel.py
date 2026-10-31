"""주간 Steam 할인 캐러셀: "이번 주 Steam 역대 최저가 N".

  python deals_carousel.py   # out/deals_carousel/ 에 샘플 생성
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

from cards import W, H, ROOT, fetch_img, cover_fit
from card_themes import BH, NB, NM, NR, ANTON, grade, shade, lines_of, fit, HANDLE
from carousel import LIME as RED, WHITE, SOFT
GOLD, INK = (245, 178, 51), (18, 16, 12)  # 할인 테마: 알트탭 레드와 어울리는 웜 골드, 그 위 글자는 진한 먹색

DATA = ROOT / "data"
GREEN = (31, 157, 85)


def plus(d, cx=100, cy=100, L=26, t=4):
    pass  # 왼쪽 위 '+' 표시는 뺐다 (거슬린다는 피드백)


def frame(img, page, total):
    d = ImageDraw.Draw(img)
    plus(d)
    d.text((W - 56, 70), f"{page}/{total}", font=NB(30), fill=GOLD, anchor="ra", stroke_width=3, stroke_fill=(10, 10, 12))
    d.text((W - 56, H - 70), HANDLE, font=NB(32), fill=GOLD, anchor="ra")
    return d


def key_art(appid, fallback):
    """글자 없는 공식 키아트(library_hero) 우선. Steam StoreBrowse API 로 정확한 주소를 얻는다."""
    import urllib.request, urllib.parse
    q = json.dumps({"ids": [{"appid": int(appid)}], "context": {"country_code": "KR"},
                    "data_request": {"include_assets": True, "include_screenshots": True}})
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=" + urllib.parse.quote(q),
            headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read())
        it = r["response"]["store_items"][0]
        assets = it.get("assets") or {}
        fmt = assets.get("asset_url_format", "")
        for k in ("library_hero_2x", "library_hero", "page_background"):
            if assets.get(k) and fmt:
                return "https://shared.akamai.steamstatic.com/store_item_assets/" + fmt.replace("${FILENAME}", assets[k])
        shots = (it.get("screenshots") or {}).get("all_ages_screenshots") or []
        if shots:
            return "https://shared.akamai.steamstatic.com/store_item_assets/" + shots[0]["filename"]
    except Exception:
        pass
    return fallback


def norm(s):
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def pick_deals(n=7):
    deals = json.loads((DATA / "deals.json").read_text(encoding="utf-8"))["deals"]
    apps = json.loads((DATA / "steam_apps.json").read_text(encoding="utf-8"))
    alld = {}
    p = DATA / "deals_all.json"
    if p.exists():
        alld = {norm(x["t"]): x for x in json.loads(p.read_text(encoding="utf-8"))["list"]}
    out = []
    for x in deals:
        app = apps.get(f"id:{x['appid']}") or {}
        extra = alld.get(norm(app.get("name"))) or alld.get(norm(x["name"])) or {}
        lo = x.get("ever_low") or extra.get("lo")
        x = {**x, "hd": key_art(x["appid"], app.get("hd") or x["img"]), "expiry": extra.get("x", ""),
             "record": bool(lo) and x["final"] <= lo * 1.02}
        score = (3 if x["record"] else 0) + (2 if x["ko"] else 0) + (1 if (x.get("meta") or 0) >= 75 else 0) \
            + (1 if x["rank"] <= 30 else 0) + x["discount"] / 100
        out.append((score, x))
    out.sort(key=lambda t: -t[0])
    chosen, cheap = [], 0
    for _, x in out:  # 가격대 섞기: 1만 원 이하는 최대 3개
        if x["final"] <= 10000:
            if cheap >= 3:
                continue
            cheap += 1
        chosen.append(x)
        if len(chosen) == n:
            break
    return chosen


def slide_cover(deals, total, line1="이번 주 Steam", line2="역대 최저가", sub="한국어 지원 · 할인율 · 마감일 한 번에"):
    img = Image.new("RGB", (W, H), (10, 10, 12))
    ims = [fetch_img(x["hd"]) for x in deals[:4]]
    for k, im in enumerate(i for i in ims if i):
        img.paste(grade(cover_fit(im, W // 2, H // 2), bright=0.75), ((k % 2) * W // 2, (k // 2) * H // 2))
    img = shade(img, 0.15, 235)
    d = frame(img, 1, total)
    d.text((76, 600), datetime.now().strftime("%m.%d") + " 기준", font=NB(36), fill=GOLD)
    d.text((76, 660), line1, font=BH(104), fill=WHITE)
    d.text((76, 894), line2, font=BH(104), fill=WHITE, anchor="ls")
    d.text((76 + d.textlength(line2 + " ", font=BH(104)), 900), str(len(deals)), font=ANTON(170), fill=GOLD, anchor="ls")
    d.text((76, 960), sub, font=NM(36), fill=SOFT)
    return img


def badge(d, x, y, text, fill, fg=WHITE, f=None):
    f = f or NB(30)
    w = d.textlength(text, font=f)
    d.rounded_rectangle((x, y, x + w + 32, y + 52), 10, fill=fill)
    d.text((x + 16, y + 26), text, font=f, fill=fg, anchor="lm")
    return x + w + 32 + 12


def slide_deal(x, page, total):
    img = Image.new("RGB", (W, H), (12, 12, 14))
    im = fetch_img(x["hd"])
    if im:
        top = grade(cover_fit(im, W, 700), mono=0.25, bright=0.9, contrast=1.05)
        img.paste(top, (0, 0))
        fade = Image.new("L", (1, 700))
        for yy in range(700):
            fade.putpixel((0, yy), int(max(0, (yy - 380) / 320) ** 1.3 * 255))
        img.paste(Image.new("RGB", (W, 700), (12, 12, 14)), (0, 0), fade.resize((W, 700)))
    d = frame(img, page, total)
    # 할인율: 큰 레드 블록
    pct = f"-{x['discount']}%"
    d.rounded_rectangle((76, 560, 76 + d.textlength(pct, font=ANTON(120)) + 48, 720), 16, fill=GOLD)
    d.text((100, 640), pct, font=ANTON(120), fill=INK, anchor="lm")
    # 게임명
    y = 760
    nf, nl = fit(d, x["name"].replace("™", "").replace("®", ""), BH, 72, W - 152, 2, 44)
    for ln in nl:
        d.text((76, y), ln, font=nf, fill=WHITE)
        y += int(nf.size * 1.22)
    # 가격
    y += 20
    orig = f"₩{x['initial']:,}"
    d.text((76, y + 30), orig, font=NM(44), fill=(150, 150, 158), anchor="lm")
    ow = d.textlength(orig, font=NM(44))
    d.line([(76, y + 32), (76 + ow, y + 32)], fill=(150, 150, 158), width=3)
    d.text((76 + ow + 28, y + 30), "→", font=NM(44), fill=(150, 150, 158), anchor="lm")
    d.text((76 + ow + 90, y + 30), f"₩{x['final']:,}", font=BH(84), fill=WHITE, anchor="lm")
    # 배지
    y += 110
    bx = 76
    if x.get("record"):
        bx = badge(d, bx, y, "역대 최저가", GOLD, INK)
    if x.get("ko"):
        bx = badge(d, bx, y, "한국어 " + ("자막·음성" if x.get("ko_voice") else "자막"), GREEN)
    if x.get("meta"):
        bx = badge(d, bx, y, f"메타 {x['meta']}", (60, 60, 68))
    # 마감·장르
    y += 86
    info = []
    if x.get("expiry"):
        m, dd = x["expiry"][5:7], x["expiry"][8:10]
        info.append(f"{int(m)}월 {int(dd)}일까지")
    if x.get("genres"):
        info.append(" · ".join(x["genres"][:2]))
    d.text((76, y), "   |   ".join(info), font=NR(34), fill=SOFT)
    return img


def slide_end(deals, total):
    im = fetch_img(deals[0]["hd"])
    img = grade(cover_fit(im, W, H), bright=0.5).filter(ImageFilter.GaussianBlur(18)) if im else Image.new("RGB", (W, H))
    img = Image.blend(img, Image.new("RGB", (W, H), (10, 10, 12)), 0.55)
    d = frame(img, total, total)
    d.text((76, 380), "세일 끝나기 전에", font=BH(84), fill=WHITE)
    d.text((76, 480), "저장해두세요", font=BH(84), fill=GOLD)
    d.text((76, 610), "가격은 게시 시점 Steam 한국 가격 기준이며", font=NR(32), fill=SOFT)
    d.text((76, 660), "할인 기간·가격은 바뀔 수 있습니다.", font=NR(32), fill=SOFT)
    d.rounded_rectangle((76, H - 330, W - 76, H - 170), 24, fill=GOLD)
    d.text((W // 2, H - 280), "매주 금요일 할인 정리", font=NB(40), fill=INK, anchor="mm")
    d.text((W // 2, H - 220), "팔로우하고 받아보기", font=BH(56), fill=INK, anchor="mm")
    return img


def story(x):
    """오늘의 딜: 인스타 스토리 1장 (9:16)."""
    SW, SH = 1080, 1920
    img = Image.new("RGB", (SW, SH), (12, 12, 14))
    im = fetch_img(x["hd"])
    if im:
        top = grade(cover_fit(im, SW, 1100), mono=0.2, bright=0.9, contrast=1.05)
        img.paste(top, (0, 0))
        fade = Image.new("L", (1, 1100))
        for yy in range(1100):
            fade.putpixel((0, yy), int(max(0, (yy - 650) / 450) ** 1.3 * 255))
        img.paste(Image.new("RGB", (SW, 1100), (12, 12, 14)), (0, 0), fade.resize((SW, 1100)))
    d = ImageDraw.Draw(img)
    plus(d, 100, 180)
    d.text((150, 180), "오늘의 딜", font=BH(56), fill=WHITE, anchor="lm", stroke_width=3, stroke_fill=(10, 10, 12))
    pct = f"-{x['discount']}%"
    d.rounded_rectangle((76, 960, 76 + d.textlength(pct, font=ANTON(150)) + 56, 1160), 18, fill=GOLD)
    d.text((104, 1060), pct, font=ANTON(150), fill=INK, anchor="lm")
    y = 1210
    nf, nl = fit(d, x["name"].replace("™", "").replace("®", ""), BH, 84, SW - 152, 2, 50)
    for ln in nl:
        d.text((76, y), ln, font=nf, fill=WHITE)
        y += int(nf.size * 1.24)
    orig = f"₩{x['initial']:,}"
    d.text((76, y + 50), orig, font=NM(48), fill=(150, 150, 158), anchor="lm")
    ow = d.textlength(orig, font=NM(48))
    d.line([(76, y + 52), (76 + ow, y + 52)], fill=(150, 150, 158), width=3)
    d.text((76 + ow + 100, y + 50), f"₩{x['final']:,}", font=BH(100), fill=WHITE, anchor="lm")
    d.text((76 + ow + 30, y + 50), "→", font=NM(48), fill=(150, 150, 158), anchor="lm")
    y += 140
    bx = 76
    if x.get("record"):
        bx = badge(d, bx, y, "역대 최저가", GOLD, INK, f=NB(36))
    if x.get("ko"):
        bx = badge(d, bx, y, "한국어 지원", GREEN, f=NB(36))
    if x.get("meta"):
        badge(d, bx, y, f"메타 {x['meta']}", (60, 60, 68), f=NB(36))
    if x.get("expiry"):
        e = datetime.strptime(x["expiry"][:10], "%Y-%m-%d")
        d.text((76, y + 110), f"{e.month}월 {e.day}일 ({'월화수목금토일'[e.weekday()]})까지 할인", font=BH(56), fill=WHITE)
    d.text((SW // 2, SH - 120), HANDLE, font=NB(38), fill=GOLD, anchor="mm")
    return img


def build(n=7, skip=(), **cover):
    deals = [x for x in pick_deals(n + len(skip)) if x["appid"] not in skip][:n]
    total = len(deals) + 2
    return [slide_cover(deals, total, **cover)] + [slide_deal(x, i + 2, total) for i, x in enumerate(deals)] + [slide_end(deals, total)], deals



WD = "월화수목금토일"


def release_story(r):
    """출시 예정: 인스타 스토리 1장 (9:16). 하이라이트 '출시예정'용 — 출시일이 지나면 하이라이트에서 내린다."""
    SW, SH = 1080, 1920
    img = Image.new("RGB", (SW, SH), (12, 12, 14))
    im = fetch_img(key_art(r["appid"], r["img"]) if r.get("appid") else r["img"])
    if im:
        top = grade(cover_fit(im, SW, 1180), mono=0.15, bright=0.9, contrast=1.05)
        img.paste(top, (0, 0))
        fade = Image.new("L", (1, 1180))
        for yy in range(1180):
            fade.putpixel((0, yy), int(max(0, (yy - 700) / 480) ** 1.3 * 255))
        img.paste(Image.new("RGB", (SW, 1180), (12, 12, 14)), (0, 0), fade.resize((SW, 1180)))
    d = ImageDraw.Draw(img)
    plus(d, 100, 180)
    d.text((150, 180), "출시 예정", font=BH(56), fill=WHITE, anchor="lm", stroke_width=3, stroke_fill=(10, 10, 12))
    day = datetime.strptime(r["date"], "%Y-%m-%d")
    chip = f"{day.month}.{day.day}"  # 하이라이트에 며칠씩 남으므로 D-day 대신 고정된 날짜
    d.rounded_rectangle((76, 1000, 76 + d.textlength(chip, font=ANTON(130)) + 56, 1170), 18, fill=RED)
    d.text((104, 1085), chip, font=ANTON(130), fill=WHITE, anchor="lm")
    y = 1220
    nf, nl = fit(d, r["title"].replace("™", "").replace("®", ""), BH, 88, SW - 152, 2, 52)
    for ln in nl:
        d.text((76, y), ln, font=nf, fill=WHITE)
        y += int(nf.size * 1.22)
    y += 30
    d.text((76, y), f"{day.month}월 {day.day}일 ({WD[day.weekday()]}) 출시", font=BH(64), fill=WHITE)
    y += 100
    meta = " · ".join(x for x in [", ".join(r.get("platforms") or []), r.get("genre") or ""] if x)
    if meta:
        f2, l2 = fit(d, meta, NM, 44, SW - 152, 2, 34)
        for ln in l2:
            d.text((76, y), ln, font=f2, fill=SOFT)
            y += int(f2.size * 1.4)
    d.text((SW // 2, SH - 120), HANDLE, font=NB(38), fill=RED, anchor="mm")
    return img

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = ROOT / "out" / "deals_carousel"
    out.mkdir(parents=True, exist_ok=True)
    slides, deals = build()
    for i, im in enumerate(slides, 1):
        im.save(out / f"{i:02d}.jpg", quality=90)
    for x in deals:
        print(f"- {x['name'][:30]} -{x['discount']}% ₩{x['final']:,} 최저:{x['record']} 한글:{x['ko']} 메타:{x.get('meta')} 마감:{x['expiry']}")
    s = 260
    sheet = Image.new("RGB", (len(slides) * (s + 8) + 8, int(s * 1.25) + 16), (230, 230, 230))
    for i, im in enumerate(slides):
        sheet.paste(im.resize((s, int(s * 1.25))), (8 + i * (s + 8), 8))
    sheet.save(out / "strip.jpg", quality=88)
