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
        eye_mid, nose = (float(f[4]) + float(f[6])) / 2, float(f[8])
        look = (nose - eye_mid) / max(fw, 1)  # +면 화면 오른쪽, -면 왼쪽을 본다
        out.append(((x + fw / 2) / small.width, (y + fh / 2) / small.height, score * (fw * fh) ** 0.5,
                    1 if look > 0.06 else -1 if look < -0.06 else 0))
    return out


def _subject(grid, ew, eh, faces):
    """주인공 위치(격자 좌표)와 바라보는 방향. 얼굴이 있으면 가장 큰 얼굴, 없으면 가장 눈에 띄는 덩어리의 무게중심."""
    if faces:
        f = faces[0]
        return f[0] * ew, f[1] * eh, f[3], True
    vals = sorted((grid[y][x] for y in range(eh) for x in range(ew)), reverse=True)
    cut = vals[max(0, len(vals) // 12 - 1)] if vals else 0  # 상위 약 8%
    sx = sy = tw = 0.0
    for y in range(eh):
        for x in range(ew):
            if grid[y][x] >= cut and cut > 0:
                sx += x * grid[y][x]; sy += y * grid[y][x]; tw += grid[y][x]
    if not tw:
        return ew / 2, eh / 2, 0, False
    return sx / tw, sy / tw, 0, False


def _compose(prof, n, win, subj, look, thirds_t, keep=()):
    """한 축(가로 또는 세로)에서 사진 구도 점수가 가장 높은 시작 위치.
    - 담긴 볼거리 (많을수록 +)
    - 가장자리가 피사체를 가르면 감점 (경계에 볼거리가 몰려 있으면 −)
    - 주인공을 삼분할 선에, 바라보는 쪽에 여백 (시선 방향 공간)
    - 주인공이 화면 밖으로 잘리면 탈락"""
    total = sum(prof) or 1
    peak = max(prof) or 1
    best, best_s = 0, -1e9
    for i in range(0, n - win + 1):
        inside = sum(prof[i:i + win]) / total
        cut = (prof[i] + prof[i + win - 1]) / (2 * peak)
        pos = (subj - i) / win  # 화면 안에서 주인공 위치 0~1
        if subj >= 0 and not (0.12 <= pos <= 0.88):
            continue
        if any(not (i + win * 0.06 <= k <= i + win * 0.94) for k in keep):  # 사람이 여럿이면 모두 화면 안에
            continue
        if subj < 0:
            comp = 0
        else:
            target = thirds_t if thirds_t is not None else (1 / 3 if look > 0 else 2 / 3 if look < 0 else
                                                            (1 / 3 if pos < 0.5 else 2 / 3))
            comp = 1 - min(1, abs(pos - target) / 0.33)
        sc = inside * 1.6 + comp * 0.3 - cut * 0.6  # 피사체를 다 담는 게 우선, 삼분할은 그다음
        if sc > best_s:
            best, best_s = i, sc
    return best


def _landscape(img, grid, ew, eh, w, h):
    """인물·주인공 없이 배경이 중심인 사진: 풍경 사진 구도로 자를 위치 (x, y) 를 고른다.
    - 좌우 대칭 장면 → 가운데 (대칭 유지)
    - 랜드마크(탑·건물처럼 세로로 솟은 볼거리) → 삼분할 세로선
    - 그 외 → 색·밝기가 가장 다채로운 구간 (밋밋한 하늘·벽만 남지 않게)
    - 위아래를 자를 땐 지평선(하늘↔땅 경계)을 삼분할 가로선에"""
    from PIL import ImageOps
    x, y = (img.width - w) // 2, (img.height - h) // 2
    sm = img.convert("RGB").resize((ew, eh), Image.BILINEAR)
    lum = sm.convert("L")
    if img.width - w > 8:
        ww = max(1, int(w * ew / img.width))
        # 좌우 대칭이면 가운데
        lp, rp = list(lum.getdata()), list(ImageOps.mirror(lum).getdata())
        sym = 1 - sum(abs(a - b) for a, b in zip(lp, rp)) / (255 * len(lp))
        if sym > 0.9:
            return x, y
        cols = [sum(grid[yy][xx] for yy in range(eh)) for xx in range(ew)]
        mean = sum(cols) / ew or 1
        peak = max(range(ew), key=lambda i: cols[i])
        landmark = cols[peak] > mean * 2.2  # 주변보다 확 튀는 세로 볼거리
        hsv = sm.convert("HSV")
        best, best_s = (img.width - w) // 2, -1e9
        for i in range(0, ew - ww + 1):
            box = (i, 0, i + ww, eh)
            hist = hsv.crop(box).convert("RGB").quantize(16).histogram()[:16]
            tot = sum(hist) or 1
            import math
            ent = -sum(c / tot * math.log(c / tot) for c in hist if c) / math.log(16)  # 다채로움 0~1
            inside = sum(cols[i:i + ww]) / (sum(cols) or 1)
            mid = 1 - abs((i + ww / 2) / ew - 0.5) * 2  # 거리·복도처럼 소실점이 가운데인 풍경이 많아 가운데 쪽 약간 선호
            sc = ent * 0.5 + inside * 0.8 + mid * 0.45
            if landmark:
                pos = (peak - i) / ww
                if not 0.15 <= pos <= 0.85:
                    continue
                sc += (1 - min(1, min(abs(pos - 1 / 3), abs(pos - 2 / 3)) / 0.33)) * 0.5
            if sc > best_s:
                best, best_s = int(i * img.width / ew), sc
        x = min(img.width - w, best)
    elif img.height - h > 8:
        wh = max(1, int(h * eh / img.height))
        rows = [sum(lum.getpixel((xx, yy)) for xx in range(ew)) / ew for yy in range(eh)]
        # 지평선: 위아래 밝기 차가 가장 큰 가로줄
        hz = max(range(2, eh - 2), key=lambda yy: abs(sum(rows[yy - 2:yy]) - sum(rows[yy:yy + 2])))
        lower_busy = sum(map(sum, grid[hz:])) > sum(map(sum, grid[:hz]))
        target = 1 / 3 if lower_busy else 2 / 3  # 땅이 볼거리면 지평선을 위 1/3, 하늘이 볼거리면 아래 1/3
        start = int(hz - target * wh)
        y = int(max(0, min(eh - wh, start)) * img.height / eh)
        y = min(img.height - h, y)
    return x, y


_SSD = []
SSD_CLASSES = ["bg", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat", "chair", "cow", "diningtable",
               "dog", "horse", "motorbike", "person", "pottedplant", "sheep", "sofa", "train", "tvmonitor"]
SUBJECT_CLASSES = {"aeroplane", "bicycle", "bird", "boat", "bus", "car", "cat", "cow", "dog", "horse", "motorbike",
                   "person", "sheep", "train"}  # 가구·화분·병 같은 소품은 주인공으로 치지 않는다


def _objects(img):
    """사진의 주인공 후보 물체 (사람·동물·탈것) — MobileNet-SSD (models/mobilenet_ssd.*, MIT).
    반환: [(x0, y0, x1, y1, 넓이비율, 이름)] 화면 비율 좌표, 큰 것부터."""
    try:
        import cv2, numpy as np
        if not _SSD:
            m = ROOT / "models"
            if not (m / "mobilenet_ssd.caffemodel").exists():
                return []
            _SSD.append(cv2.dnn.readNetFromCaffe(str(m / "mobilenet_ssd.prototxt"), str(m / "mobilenet_ssd.caffemodel")))
        arr = np.array(img.convert("RGB").resize((300, 300)))[:, :, ::-1]
        _SSD[0].setInput(cv2.dnn.blobFromImage(arr, 0.007843, (300, 300), 127.5))
        det = _SSD[0].forward()[0, 0]
    except Exception:
        return []
    out = []
    for r in det:
        name = SSD_CLASSES[int(r[1])] if 0 <= int(r[1]) < len(SSD_CLASSES) else ""
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in r[3:7])
        area = (x1 - x0) * (y1 - y0)
        if r[2] > 0.45 and name in SUBJECT_CLASSES and area >= 0.04:  # 너무 작은 건 배경의 일부
            out.append((x0, y0, x1, y1, area, name))
    return sorted(out, key=lambda o: -o[4])


def _is_scenery(grid, ew, eh):
    """볼거리가 한곳에 몰려 있지 않고 넓게 퍼져 있으면 '배경 위주' 사진."""
    vals = sorted((v for row in grid for v in row), reverse=True)
    tot = sum(vals) or 1
    top = sum(vals[: max(1, len(vals) // 12)])  # 상위 약 8% 칸이 차지하는 비율
    return top / tot < 0.3


def cover_fit(img, w, h, smart=True):
    """w×h 를 꽉 채우도록 확대 후 자른다. smart=True 면 사진 구도 원칙으로 자를 위치를 고른다:
    삼분할 · 시선 방향 여백 · 머리 위 여백 · 피사체를 가장자리로 가르지 않기."""
    r = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    if smart and (img.width - w > 8 or img.height - h > 8):
        try:
            grid, ew, eh = _interest(img)
            # 화면 맨 가장자리(바깥 15%)의 얼굴은 주인공이 아니다 — 1인칭 슈팅 화면의 옆 병사 등.
            # 게임 스크린샷은 보통 화면 가운데(조준점·캐릭터)를 중심으로 구성되므로 가장자리 인물을 따라가지 않는다
            faces = sorted([f for f in _people(img) if 0.15 <= f[0] <= 0.85], key=lambda f: -f[2])
            objs = [o for o in ([] if faces else _objects(img)) if 0.15 <= (o[0] + o[2]) / 2 <= 0.85]
            if not faces and not objs:  # 사람·동물·탈것 같은 주인공이 없으면 배경 위주 → 풍경 구도
                # 사람 선호 구도 모델(FocalNet)이 우선, 실패하면 손으로 짠 풍경 규칙
                from focal import best_crop
                c = best_crop(img, w / h, min_scale=1.0)
                if c:
                    return img.crop((c[0], c[1], c[0] + c[2], c[1] + c[3])).resize((w, h), Image.LANCZOS)
                x, y = _landscape(img, grid, ew, eh, w, h)
                return img.crop((x, y, x + w, y + h))
            subx, suby, look, is_face = _subject(grid, ew, eh, faces)
            if objs:  # 얼굴은 없지만 주인공 물체가 있으면 그 물체 중심을 주인공으로
                o = objs[0]
                subx, suby = (o[0] + o[2]) / 2 * ew, (o[1] + o[3]) / 2 * eh
            sx, sy = ew / img.width, eh / img.height
            ww, wh = max(1, int(w * sx)), max(1, int(h * sy))
            if img.width - w > 8:  # 가로로 남는다 (16:9 → 세로 화면): 삼분할 + 시선 방향 여백
                cols = [sum(grid[yy][xx] for yy in range(eh)) for xx in range(ew)]
                best = _compose(cols, ew, ww, subx, look, None, [f[0] * ew for f in faces[1:4]])
                x = min(img.width - w, int(best / sx))
            else:  # 세로로 남는다: 얼굴은 위 1/3 (머리 위 여백), 사물은 가운데보다 약간 위
                rows = [sum(grid[yy]) for yy in range(eh)]
                best = _compose(rows, eh, wh, suby, 0, 1 / 3 if is_face else 0.42, [f[1] * eh for f in faces[1:4]])
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
