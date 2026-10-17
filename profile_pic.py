"""인스타그램 프로필 사진 3종 (1080x1080, 원형으로 잘려도 안전하게 가운데 정렬)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "out" / "profile"
S = 1080
RED, BLACK, WHITE = (229, 72, 77), (15, 15, 18), (255, 255, 255)


def mono(size):
    for p in ("C:/Windows/Fonts/consolab.ttf", str(ROOT / "fonts/Anton-Regular.ttf")):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass


def wordmark(d, cx, cy, size, fg, accent):
    """사이트 로고와 같은 'alt+tab_' (가운데 정렬)."""
    f = mono(size)
    parts = [("alt", fg), ("+", accent), ("tab", fg)]
    total = sum(d.textlength(t, font=f) for t, _ in parts) + size * 0.62
    x = cx - total / 2
    for t, c in parts:
        d.text((x, cy), t, font=f, fill=c, anchor="lm")
        x += d.textlength(t, font=f)
    d.rectangle((x + size * 0.08, cy + size * 0.30, x + size * 0.62, cy + size * 0.40), fill=accent)


def v1():  # 검정 배경 + 워드마크 (사이트와 동일)
    im = Image.new("RGB", (S, S), BLACK)
    wordmark(ImageDraw.Draw(im), S / 2, S / 2, 190, WHITE, RED)
    return im


def v2():  # 레드 배경 + 2단 "alt / +tab" (작게 봐도 읽히게)
    im = Image.new("RGB", (S, S), RED)
    d = ImageDraw.Draw(im)
    f = mono(290)
    w1, w2 = d.textlength("alt", font=f), d.textlength("+tab", font=f)
    x = S / 2 - w2 / 2
    d.text((x + (w2 - w1), S / 2 - 140), "alt", font=f, fill=WHITE, anchor="lm")   # 오른쪽 정렬
    d.text((x, S / 2 + 140), "+", font=f, fill=BLACK, anchor="lm")
    d.text((x + d.textlength("+", font=f), S / 2 + 140), "tab", font=f, fill=WHITE, anchor="lm")
    return im


def v3():  # 모노그램 "a+t" + 커서 (가장 작게 봐도 식별)
    im = Image.new("RGB", (S, S), BLACK)
    d = ImageDraw.Draw(im)
    f = mono(400)
    parts = [("a", WHITE), ("+", RED), ("t", WHITE)]
    total = sum(d.textlength(t, font=f) for t, _ in parts)
    x = S / 2 - total / 2
    for t, c in parts:
        d.text((x, S / 2 - 30), t, font=f, fill=c, anchor="lm")
        x += d.textlength(t, font=f)
    d.rectangle((S / 2 - 140, S / 2 + 190, S / 2 + 140, S / 2 + 228), fill=RED)  # 커서
    return im


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    imgs = {"profile_1_wordmark": v1(), "profile_2_red": v2(), "profile_3_icon": v3()}
    for n, im in imgs.items():
        im.save(OUT / f"{n}.png")
    # 미리보기: 원형으로 잘린 모습을 큰 크기·피드 크기로
    pv = Image.new("RGB", (3 * 380 + 40, 560), (245, 245, 245))
    mask = Image.new("L", (360, 360), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 359, 359), fill=255)
    small = Image.new("L", (90, 90), 0)
    ImageDraw.Draw(small).ellipse((0, 0, 89, 89), fill=255)
    for i, im in enumerate(imgs.values()):
        pv.paste(im.resize((360, 360), Image.LANCZOS), (20 + i * 380, 20), mask)
        pv.paste(im.resize((90, 90), Image.LANCZOS), (20 + i * 380 + 135, 420), small)
    pv.save(OUT / "preview.png")
    print("완료:", [p.name for p in OUT.glob("*.png")])
