"""인스타그램 카드뉴스 이미지 생성 (1080x1350, 4:5).

  python cards.py sample   # out/cards/ 에 샘플 카드 생성
"""
import io, json, sys, time, urllib.request, textwrap
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "out" / "cards"
W, H = 1080, 1350
BG, FG, MUT, ACC = (15, 15, 18), (237, 237, 240), (150, 150, 163), (229, 72, 77)
TRUST = {"공식": (31, 157, 85), "확인": (37, 99, 235), "보도": (120, 120, 132), "루머": (217, 119, 6)}
FONT_DIRS = [ROOT / "fonts", Path("C:/Windows/Fonts"), Path("/usr/share/fonts/opentype/noto"), Path("/usr/share/fonts/truetype/noto")]


def font(name, size):
    for d in FONT_DIRS:
        for f in (d / name, *d.glob(f"**/{name}")):
            if Path(f).exists():
                return ImageFont.truetype(str(f), size)
    raise FileNotFoundError(name)


def F(weight, size):
    return font({"b": "NotoSansKR-Bold.ttf", "m": "NotoSansKR-Medium.ttf", "r": "NotoSansKR-Regular.ttf"}[weight], size)


def MONO(size):
    for n in ("consolab.ttf", "DejaVuSansMono-Bold.ttf", "NotoSansMono-Bold.ttf"):
        try:
            return font(n, size)
        except FileNotFoundError:
            pass
    return F("b", size)


def fetch_img(url):
    if url and not url.startswith("http"):  # 사이트에 함께 올리는 로컬 이미지 (media/...)
        try:
            return Image.open(ROOT / url).convert("RGB")
        except Exception:
            return None
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read()
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def _interest(img):
    """작게 줄인 이미지의 '눈에 띄는 곳' 지도 (간단한 시각 주목도):
    - 주변과 색·밝기가 확 다른 곳 (원본 − 크게 흐린 배경) → 인물·주요 사물이 높게, 커튼·풀숲 같은 반복 무늬는 낮게
    - 사람 피부색 가점, 가운데 약간 가점"""
    from PIL import ImageFilter, ImageChops
    sm = img.convert("RGB").resize((64, max(1, int(64 * img.height / img.width))), Image.BILINEAR)
    bg = sm.filter(ImageFilter.GaussianBlur(10))
    diff = ImageChops.difference(sm, bg).convert("L").filter(ImageFilter.GaussianBlur(2))
    ew, eh = sm.size
    dv, px = list(diff.getdata()), list(sm.getdata())
    grid = [[0.0] * ew for _ in range(eh)]
    for yy in range(eh):
        for xx in range(ew):
            k = yy * ew + xx
            r, g, b = px[k]
            skin = 1.6 if (r > 95 and g > 40 and b > 20 and r > g > b and r - b > 15 and abs(r - g) > 12) else 1.0
            cx = 1 - abs(xx - ew / 2) / ew * 0.8
            grid[yy][xx] = (dv[k] ** 1.5) * skin * cx
    return grid, ew, eh


def _people(img):
    """사람 얼굴 위치 (OpenCV YuNet 딥러닝 얼굴 인식, models/face_yunet.onnx). 반환: [(x중심비율, y중심비율, 점수)]
    헬멧·고글을 쓴 게임 캐릭터 얼굴도 잡히고, 불꽃·무늬 오탐이 적다."""
    model = ROOT / "models" / "face_yunet.onnx"
    try:
        import cv2, numpy as np
        if not model.exists():
            return []
        small = img.convert("RGB").resize((960, max(1, int(960 * img.height / img.width))))
        arr = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2BGR)
        det = cv2.FaceDetectorYN.create(str(model), "", (small.width, small.height), 0.7)
        _, faces = det.detect(arr)
    except Exception:
        return []
    if faces is None:
        return []
    out = []
    for f in faces:
        x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])
        out.append(((x + fw / 2) / small.width, (y + fh / 2) / small.height, score * (fw * fh) ** 0.5))
    return out


def cover_fit(img, w, h, smart=True):
    """w×h 를 꽉 채우도록 확대 후 자른다. smart=True 면 가운데가 아니라 '가장 볼거리 많은' 구간을 남긴다."""
    r = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    if smart and (img.width - w > 8 or img.height - h > 8):
        try:
            grid, ew, eh = _interest(img)
            faces = sorted(_people(img), key=lambda f: -f[2])  # 가장 크고 확실한 얼굴 = 주인공
            sx, sy = ew / img.width, eh / img.height
            ww, wh = max(1, int(w * sx)), max(1, int(h * sy))
            if img.width - w > 8:  # 가로로 남는다 → 가장 좋은 가로 위치
                cols = [sum(grid[yy][xx] for yy in range(eh)) for xx in range(ew)]
                rng = range(0, ew - ww + 1)
                if faces:  # 주인공 얼굴이 화면 가로 20~80% 사이에 오도록 하는 위치만 후보로
                    fx = faces[0][0] * ew
                    ok = [i for i in rng if i + ww * 0.2 <= fx <= i + ww * 0.8]
                    rng = ok or [max(0, min(ew - ww, int(fx - ww / 2)))]
                best = max(rng, key=lambda i: sum(cols[i:i + ww]))
                x = min(img.width - w, int(best / sx))
            else:  # 세로로 남는다 → 위쪽(인물 얼굴 쪽)을 약간 선호
                rows = [sum(grid[yy]) * (1.15 - 0.3 * yy / eh) for yy in range(eh)]
                rng = range(0, eh - wh + 1)
                if faces:
                    fy = faces[0][1] * eh
                    ok = [i for i in rng if i + wh * 0.15 <= fy <= i + wh * 0.6]
                    rng = ok or [max(0, min(eh - wh, int(fy - wh / 3)))]
                best = max(rng, key=lambda i: sum(rows[i:i + wh]))
                y = min(img.height - h, int(best / sy))
        except Exception:
            pass
    return img.crop((x, y, x + w, y + h))


def wrap(draw, text, fnt, width):
    """한글은 글자 단위로 줄바꿈 (단어 경계 우선)."""
    lines, cur = [], ""
    for word in text.split(" "):
        test = (cur + " " + word).strip()
        if draw.textlength(test, font=fnt) <= width:
            cur = test
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for ch in word:  # 한 단어가 너무 길면 글자 단위로
            if draw.textlength(cur + ch, font=fnt) > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    if cur:
        lines.append(cur)
    return lines


def logo(d, x, y, size=34):
    m = MONO(size)
    d.text((x, y), "alt", font=m, fill=FG)
    x += d.textlength("alt", font=m)
    d.text((x, y), "+", font=m, fill=ACC)
    x += d.textlength("+", font=m)
    d.text((x, y), "tab", font=m, fill=FG)
    x += d.textlength("tab", font=m) + 4
    d.rectangle((x, y + size * 0.95, x + size * 0.55, y + size * 0.95 + 5), fill=ACC)


def gradient(img, top, bottom=0.92, strength=245):
    """아래쪽을 어둡게 (글자 가독성)."""
    g = Image.new("L", (1, img.height))
    for yy in range(img.height):
        t = (yy / img.height - top) / (bottom - top)
        g.putpixel((0, yy), int(max(0, min(1, t)) ** 1.4 * strength))
    g = g.resize(img.size)
    dark = Image.new("RGB", img.size, BG)
    return Image.composite(dark, img, g)


def badge(d, x, y, text, color, f=None):
    f = f or F("b", 30)
    w = d.textlength(text, font=f)
    d.rounded_rectangle((x, y, x + w + 28, y + 48), 8, fill=color)
    d.text((x + 14, y + 5), text, font=f, fill=(255, 255, 255))
    return x + w + 28 + 12


def news_card(a, idx, total):
    img = fetch_img(a.get("img_hd") or a.get("img") or "") if a.get("img") else None
    card = Image.new("RGB", (W, H), BG)
    if img:
        card.paste(cover_fit(img, W, 780), (0, 0))
        card = gradient(card, 0.30, 0.60)
    else:  # 이미지 없으면 카테고리 색 그라데이션
        d0 = ImageDraw.Draw(card)
        for yy in range(780):
            t = yy / 780
            d0.line([(0, yy), (W, yy)], fill=tuple(int(ACC[i] * (1 - t) * 0.55 + BG[i] * (1 - (1 - t) * 0.55)) for i in range(3)))
    d = ImageDraw.Draw(card)
    logo(d, 56, 52)
    pg = f"{idx}/{total}"
    d.text((W - 56 - d.textlength(pg, font=F("m", 30)), 54), pg, font=F("m", 30), fill=FG)
    # 배지
    x = badge(d, 56, 640, a.get("trust", "보도"), TRUST.get(a.get("trust"), MUT))
    cat = a.get("category", "")
    plats = " · ".join(a.get("platforms", [])[:3])
    d.text((x, 646), f"{cat}{'  ' + plats if plats else ''}", font=F("m", 30), fill=FG)
    # 제목
    tf = F("b", 64)
    lines = wrap(d, a["title"], tf, W - 112)[:3]
    y = 720
    for ln in lines:
        d.text((56, y), ln, font=tf, fill=FG)
        y += 84
    # 3줄 요약
    y += 26
    d.rectangle((56, y, 62, y + 3 * 70 - 20), fill=ACC)
    sf = F("r", 34)
    for t in a.get("tldr", [])[:3]:
        ln = wrap(d, t, sf, W - 150)
        d.text((86, y), ln[0] + ("…" if len(ln) > 1 else ""), font=sf, fill=(205, 205, 212))
        y += 70
    # 하단
    d.line([(56, H - 110), (W - 56, H - 110)], fill=(45, 45, 52), width=2)
    src = a.get("img_src") or ""
    d.text((56, H - 88), f"출처 {a['sources'][0]['name'] if a.get('sources') else ''}" + (f" · 이미지 {src}" if src else ""),
           font=F("r", 26), fill=MUT)
    more = "매일 게임 소식은 팔로우 →"
    d.text((W - 56 - d.textlength(more, font=F("m", 26)), H - 88), more, font=F("m", 26), fill=FG)
    return card


def cover_card(arts, title="오늘의 게임 브리핑", date=None):
    card = Image.new("RGB", (W, H), BG)
    # 배경: 기사 이미지 4장 모자이크 + 블러
    imgs = [i for i in (fetch_img(a.get("img_hd") or a["img"]) for a in arts if a.get("img")) if i][:4]
    for k, im in enumerate(imgs):
        card.paste(cover_fit(im, W // 2, H // 2), ((k % 2) * W // 2, (k // 2) * H // 2))
    card = card.filter(ImageFilter.GaussianBlur(6))
    card = Image.blend(card, Image.new("RGB", (W, H), BG), 0.72)
    d = ImageDraw.Draw(card)
    logo(d, 56, 52, 40)
    date = date or datetime.now().strftime("%Y.%m.%d")
    d.text((56, 380), date, font=MONO(44), fill=ACC)
    tf = F("b", 104)
    y = 450
    for ln in wrap(d, title, tf, W - 112):
        d.text((56, y), ln, font=tf, fill=FG)
        y += 130
    y += 30
    lf = F("m", 38)
    for i, a in enumerate(arts[:5], 1):
        t = wrap(d, a["title"], lf, W - 180)[0]
        d.text((56, y), f"{i:02d}", font=MONO(38), fill=ACC)
        d.text((130, y - 4), t + ("…" if len(wrap(d, a["title"], lf, W - 180)) > 1 else ""), font=lf, fill=FG)
        y += 74
    d.text((56, H - 100), "업무 중 잠깐, 게임 소식 3줄", font=F("m", 32), fill=MUT)
    sw = "넘겨보기 →"
    d.text((W - 56 - d.textlength(sw, font=F("b", 34)), H - 102), sw, font=F("b", 34), fill=FG)
    return card


def deal_card(deals):
    card = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(card)
    logo(d, 56, 52)
    d.text((56, 140), "이번 주 Steam 할인", font=F("b", 72), fill=FG)
    d.text((56, 240), "역대 최저가 · 한국어 지원 위주", font=F("m", 34), fill=MUT)
    y = 320
    for x in deals[:5]:
        im = fetch_img(x["img"])
        if im:
            card.paste(cover_fit(im, 300, 140), (56, y))
        d.rounded_rectangle((56 + 300 - 110, y + 92, 56 + 300 - 8, y + 134), 6, fill=(76, 107, 34))
        d.text((56 + 300 - 100, y + 94), f"-{x['discount']}%", font=F("b", 30), fill=(190, 238, 17))
        nml = wrap(d, x["name"].replace("™", ""), F("b", 38), W - 460 - 120)
        nm = nml[0] + ("…" if len(nml) > 1 else "")
        d.text((390, y + 6), nm, font=F("b", 38), fill=FG)
        d.text((390, y + 62), f"₩{x['initial']:,}", font=F("r", 28), fill=MUT)
        sw = d.textlength(f"₩{x['initial']:,}", font=F("r", 28))
        d.line([(390, y + 80), (390 + sw, y + 80)], fill=MUT, width=2)
        d.text((390 + sw + 16, y + 52), f"₩{x['final']:,}", font=F("b", 40), fill=FG)
        if x.get("ko"):
            badge(d, W - 56 - 92, y + 6, "한글", (31, 157, 85), F("b", 26))
        y += 190
    d.text((56, H - 88), "가격은 게시 시점 기준", font=F("r", 26), fill=MUT)
    return card


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    arts = [a for a in json.loads((ROOT / "data/articles.json").read_text(encoding="utf-8"))
            if a.get("status", "published") == "published" and isinstance(a.get("title"), str)]
    arts.sort(key=lambda a: -a["ts"])
    pick = [a for a in arts if a.get("img")][:4] + [a for a in arts if not a.get("img")][:1]
    cover_card(pick).save(OUT / "01_cover.jpg", quality=92)
    for i, a in enumerate(pick, 1):
        news_card(a, i, len(pick)).save(OUT / f"{i + 1:02d}_news.jpg", quality=92)
    deals = json.loads((ROOT / "data/deals.json").read_text(encoding="utf-8"))["deals"]
    good = sorted([x for x in deals if x.get("ko")], key=lambda x: -x["discount"])[:5]
    deal_card(good).save(OUT / "07_deals.jpg", quality=92)
    print("생성:", sorted(p.name for p in OUT.glob("*.jpg")))
