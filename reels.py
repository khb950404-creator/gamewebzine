"""기사 1건 → 인스타 릴스 영상 (1080x1920, 약 11초, 무음).

  python reels.py [기사ID]   # out/reels/<ID>.mp4
"""
import json, os, shutil, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

from cards import ROOT, fetch_img, cover_fit
from card_themes import BH, NB, NM, ANTON, grade, lines_of, HANDLE
from carousel import LIME as RED, WHITE, SOFT, best_image
from ig_post import to_card_tone

RW, RH, FPS = 1080, 1920, 24
T_TITLE, T_POINTS, T_END = 3.5, 5.6, 2.0  # 장면 길이(초)
BG = (12, 12, 14)


def ffmpeg_bin():
    for p in (shutil.which("ffmpeg"), "D:/ffmpeg-master/bin/ffmpeg.exe"):
        if p and Path(p).exists():
            return p
    raise FileNotFoundError("ffmpeg")


def ease(t):  # 0~1 → 부드러운 감속
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def plus(d, cx=100, cy=150, L=26, t=4):
    d.rectangle((cx - L, cy - t, cx + L, cy + t), fill=RED)
    d.rectangle((cx - t, cy - L, cx + t, cy + L), fill=RED)


def text_layer(draw_fn):
    layer = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return layer


def fade_in(layer, alpha, dy):
    """레이어를 alpha(0~1) 만큼 보이게, dy 픽셀 아래에서 올라오게."""
    if alpha <= 0:
        return None
    a = layer.getchannel("A").point(lambda v: int(v * alpha))
    layer = layer.copy()
    layer.putalpha(a)
    out = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    out.paste(layer, (0, int(dy)), layer)
    return out


def render(a, out_path):
    src = fetch_img(best_image(a)) or Image.new("RGB", (RW, RH), (40, 40, 44))
    big = grade(cover_fit(src, int(RW * 1.15), int(RH * 1.15)), mono=0.35, bright=0.8)
    blurred = Image.blend(grade(cover_fit(src, RW, RH), bright=0.5).filter(ImageFilter.GaussianBlur(22)),
                          Image.new("RGB", (RW, RH), BG), 0.55)
    # 아래쪽 어둡게
    shade = Image.new("L", (1, RH))
    for y in range(RH):
        shade.putpixel((0, y), int(max(0, (y / RH - 0.28) / 0.72) ** 1.1 * 245))
    shade = shade.resize((RW, RH))

    title = [ln for ln in lines_of(ImageDraw.Draw(Image.new("RGB", (1, 1))), a["title"].replace("…", "..."), BH(104), RW - 152, 4)]
    title_layers = []
    y0 = RH - 520 - len(title) * 124
    for i, ln in enumerate(title):
        title_layers.append(text_layer(lambda d, ln=ln, y=y0 + i * 124: d.text((76, y), ln, font=BH(104), fill=WHITE)))
    label = text_layer(lambda d: d.text((76, y0 - 80), f"{a.get('trust')} · {a.get('category')}", font=NB(40), fill=RED))

    points = [to_card_tone(t) for t in a.get("tldr", [])[:3]]
    point_layers, y = [], 560
    for i, t in enumerate(points, 1):
        ls = lines_of(ImageDraw.Draw(Image.new("RGB", (1, 1))), t, NB(54), RW - 260, 3)
        def draw(d, i=i, ls=ls, y=y):
            d.text((76, y - 6), f"{i:02d}", font=ANTON(78), fill=RED)
            for k, ln in enumerate(ls):
                d.text((200, y + k * 74), ln, font=NB(54), fill=WHITE)
        point_layers.append(text_layer(draw))
        y += len(ls) * 74 + 90
    head = text_layer(lambda d: (d.rectangle((76, 418, 92, 466), fill=RED), d.text((112, 410), "핵심 요약", font=BH(60), fill=RED)))

    def frame_static(img, page):
        d = ImageDraw.Draw(img)
        plus(d)
        d.text((RW // 2, RH - 110), HANDLE, font=NB(40), fill=RED, anchor="mm")

    n_title, n_points, n_end = int(T_TITLE * FPS), int(T_POINTS * FPS), int(T_END * FPS)
    from bgm import make as make_bgm  # 직접 합성한 잔잔한 배경음 (저작권 걱정 없음)
    music = Path(out_path).with_suffix(".wav")
    make_bgm(music, seconds=T_TITLE + T_POINTS + T_END + 0.5, key=a["id"])
    total = n_title + n_points + n_end
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{RW}x{RH}", "-r", str(FPS),
           "-i", "-", "-i", str(music), "-shortest",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "21", "-profile:v", "high",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in range(total):
        t = f / FPS
        if f < n_title:  # 장면 1: 이미지 확대 + 제목 등장
            z = 1 + 0.10 * (f / n_title)
            w, h = int(RW * z), int(RH * z)
            im = big.resize((w, h), Image.BILINEAR).crop(((w - RW) // 2, (h - RH) // 2, (w - RW) // 2 + RW, (h - RH) // 2 + RH))
            im = Image.composite(Image.new("RGB", (RW, RH), BG), im, shade).convert("RGBA")
            for lay, start in [(label, 0.2)] + [(l, 0.45 + i * 0.22) for i, l in enumerate(title_layers)]:
                e = ease((t - start) / 0.5)
                o = fade_in(lay, e, (1 - e) * 60)
                if o:
                    im.alpha_composite(o)
        elif f < n_title + n_points:  # 장면 2: 요약 3줄 순서대로
            tt = t - T_TITLE
            im = blurred.convert("RGBA")
            o = fade_in(head, ease(tt / 0.4), 0)
            if o:
                im.alpha_composite(o)
            for i, lay in enumerate(point_layers):
                e = ease((tt - 0.3 - i * 1.5) / 0.5)
                o = fade_in(lay, e, (1 - e) * 50)
                if o:
                    im.alpha_composite(o)
        else:  # 장면 3: 마무리
            tt = t - T_TITLE - T_POINTS
            im = blurred.convert("RGBA")
            e = ease(tt / 0.5)
            end = text_layer(lambda d: (d.text((RW // 2, 820), "매일 게임 소식 3줄", font=BH(84), fill=WHITE, anchor="mm"),
                                        d.text((RW // 2, 930), "업무 중 잠깐, Alt+Tab", font=NM(46), fill=SOFT, anchor="mm")))
            o = fade_in(end, e, (1 - e) * 40)
            if o:
                im.alpha_composite(o)
        im = im.convert("RGB")
        frame_static(im, None)
        proc.stdin.write(im.tobytes())
    proc.stdin.close()
    proc.wait()
    music.unlink(missing_ok=True)
    return out_path


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    arts = {a["id"]: a for a in json.loads((ROOT / "data/articles.json").read_text(encoding="utf-8"))}
    ids = sys.argv[1:] or ["5e9b636987"]
    out = ROOT / "out" / "reels"
    out.mkdir(parents=True, exist_ok=True)
    for i in ids:
        p = render(arts[i], out / f"{i}.mp4")
        print(p, f"{p.stat().st_size / 1e6:.1f}MB")
