"""인스타 릴스 영상 (1080x1920, 배경음 포함).

  render(기사, 경로, lang="ko")   뉴스 릴스 ~12초: 훅(1.3초) → 제목 → 핵심 3줄 → 마무리
  render_deals(딜목록, 경로)       할인 릴스: 훅 → 게임별 1.6초씩 → 마무리 (골드 테마)

  python reels.py [기사ID]        # out/reels/ 샘플
"""
import json, re, shutil, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

from cards import ROOT, fetch_img, cover_fit
from card_themes import BH, NB, NM, ANTON, grade, fit, HANDLE
from carousel import LIME as RED, WHITE, SOFT, best_image
from ig_post import to_card_tone

RW, RH, FPS = 1080, 1920, 24
BG = (12, 12, 14)
GOLD, INK = (245, 178, 51), (18, 16, 12)
TXT = {
    "ko": {"key": "핵심 요약", "end1": "매일 게임 소식 3줄", "end2": "업무 중 잠깐, Alt+Tab"},
    "en": {"key": "KEY POINTS", "end1": "Game news in 3 lines", "end2": "Every day · Alt+Tab"},
}
PROBE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def ffmpeg_bin():
    for p in (shutil.which("ffmpeg"), "D:/ffmpeg-master/bin/ffmpeg.exe"):
        if p and Path(p).exists():
            return p
    try:  # 서버에 ffmpeg 가 없으면 pip 패키지(imageio-ffmpeg)의 정적 바이너리
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise FileNotFoundError("ffmpeg")


def ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def pop(t):  # 살짝 튀어나왔다 들어가는 크기 변화 (훅 문구용)
    t = max(0.0, min(1.0, t))
    return 1 + 0.18 * (1 - t) ** 2 - 0.18 * (1 - t) ** 6


SAFE_R, SAFE_B, SAFE_T = RW - 190, RH - 470, 260  # 이 안에만 글자를 둔다


def plus(d, color, cx=100, cy=150, L=26, t=4):
    d.rectangle((cx - L, cy - t, cx + L, cy + t), fill=color)
    d.rectangle((cx - t, cy - L, cx + t, cy + L), fill=color)


def layer(fn):
    im = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    fn(ImageDraw.Draw(im))
    return im


def show(base, lay, alpha=1.0, dy=0, scale=1.0):
    if alpha <= 0:
        return
    if scale != 1.0:
        w, h = int(RW * scale), int(RH * scale)
        lay = lay.resize((w, h), Image.BILINEAR).crop(((w - RW) // 2, (h - RH) // 2, (w - RW) // 2 + RW, (h - RH) // 2 + RH))
    if alpha < 1:
        lay = lay.copy()
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * alpha)))
    if dy:
        moved = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        moved.paste(lay, (0, int(dy)), lay)
        lay = moved
    base.alpha_composite(lay)


def shade_mask(start=0.28, strength=245):
    m = Image.new("L", (1, RH))
    for y in range(RH):
        m.putpixel((0, y), int(max(0, (y / RH - start) / (1 - start)) ** 1.1 * strength))
    return m.resize((RW, RH))


def zoomed(big, z):
    w, h = int(RW * z), int(RH * z)
    return big.resize((w, h), Image.BILINEAR).crop(((w - RW) // 2, (h - RH) // 2, (w - RW) // 2 + RW, (h - RH) // 2 + RH))


def hook_layer(text, accent, ink=WHITE):
    """화면 가운데 큰 훅 문구 (형광펜 띠 없이 흰 글자 + 그림자)."""
    f, ls = fit(PROBE, text, BH, 132, RW - 140, 2, 72)
    lh = int(f.size * 1.22)
    y0 = RH // 2 - len(ls) * lh // 2 - 60

    def draw(d):
        for i, ln in enumerate(ls):
            w = d.textlength(ln, font=f)
            x = (RW - w) / 2
            y = y0 + i * lh
            d.text((x + 4, y + 5), ln, font=f, fill=(0, 0, 0, 170))
            d.text((x, y), ln, font=f, fill=WHITE)
    return layer(draw)


class Video:
    def __init__(self, out_path, seconds, key):
        from bgm import make as make_bgm  # 곡은 게시물 폴더 이름(=큐 id)으로 고른다 → 게시할 때 같은 곡을 캡션에 표기
        self.music = Path(out_path).with_suffix(".wav")
        make_bgm(self.music, seconds=seconds + 0.5, key=Path(out_path).parent.name or key)
        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{RW}x{RH}",
               "-r", str(FPS), "-i", "-", "-i", str(self.music), "-shortest",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "21", "-profile:v", "high",
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", str(out_path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, im):
        self.proc.stdin.write(im.convert("RGB").tobytes())

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()
        self.music.unlink(missing_ok=True)


def default_hook(a, lang):
    if lang == "en":
        return (a.get("hook_en") or (a.get("title_en") or a["title"]).split(":")[0][:40]).replace("…", "...")
    if a.get("hook"):
        return a["hook"].replace("…", "...")
    # 훅이 없는 예전 기사: '게임명 + 제목 끝 핵심어' (예: '캐슬바니아 출시')
    game = re.split(r"[:：\-–]", (a.get("games") or [""])[0])[0].strip()
    last = re.sub(r"[^가-힣A-Za-z0-9]", " ", a["title"]).split()
    last = last[-1] if last else ""
    if game and last and last not in game:
        return f"{game} {last}"
    t = re.split(r"[,…·]", a["title"])[0].strip()
    return t if len(t) <= 16 else t[:16]


def render(a, out_path, lang="ko", handle=None):
    T = TXT[lang]
    handle = handle or HANDLE
    title_txt = (a.get("title_en") if lang == "en" else None) or a["title"]
    tldr = (a.get("tldr_en") if lang == "en" else None) or a.get("tldr", [])
    points = tldr[:3] if lang == "en" else [to_card_tone(t) for t in tldr[:3]]

    src = fetch_img(best_image(a)) or Image.new("RGB", (RW, RH), (40, 40, 44))
    if a.get("esports"):  # 팀 로고 표지: 잘리지 않게 가로에 맞춰 위쪽에 (색 유지)
        bw, bh = int(RW * 1.2), int(RH * 1.2)
        canvas = Image.new("RGB", (bw, bh), BG)
        sc = src.resize((int(RW * 0.95), int(src.height * RW * 0.95 / src.width)), Image.LANCZOS)
        canvas.paste(sc, ((bw - sc.width) // 2, int(bh * 0.18)))
        big = canvas
    else:
        big = grade(cover_fit(src, int(RW * 1.2), int(RH * 1.2)), mono=0.35, bright=0.8)
    dark = Image.new("RGB", (RW, RH), BG)
    blurred = Image.blend(grade(cover_fit(src, RW, RH), bright=0.5).filter(ImageFilter.GaussianBlur(22)), dark, 0.55)
    shade = shade_mask()

    hook = hook_layer(default_hook(a, lang), RED)
    tf, title = fit(PROBE, title_txt.replace("…", "...").replace("·", ", ").replace("ㆍ", ", "), BH, 92 if lang == "ko" else 84, SAFE_R - 76, 4)
    lh = int(tf.size * 1.19)
    y0 = RH - 520 - len(title) * lh
    title_ls = [layer(lambda d, ln=ln, y=y0 + i * lh: d.text((76, y), ln, font=tf, fill=WHITE)) for i, ln in enumerate(title)]
    label = layer(lambda d: d.text((76, y0 - 80), f"{a.get('trust')} · {a.get('category')}" if lang == "ko"
                                   else (a.get("category_en") or "GAME NEWS"), font=NB(40), fill=RED))
    # 요약: 조금 작고 얇은 글씨(NM)로, 세 줄이 안전 영역 안에 다 들어가게 크기를 맞춘다
    size = 46 if lang == "ko" else 44
    while True:
        blocks = [fit(PROBE, t, NM, size, SAFE_R - 200, 5, 30) for t in points]
        need = sum(len(ls) * int(pf.size * 1.42) + 80 for pf, ls in blocks)
        if 540 + need <= SAFE_B or size <= 32:
            break
        size -= 2
    point_ls, y = [], 540
    for i, (t, (pf, ls)) in enumerate(zip(points, blocks), 1):
        plh = int(pf.size * 1.42)

        def draw(d, i=i, ls=ls, y=y, pf=pf, plh=plh):
            d.text((76, y - 4), f"{i:02d}", font=ANTON(66), fill=RED)
            for k, ln in enumerate(ls):
                d.text((200, y + k * plh), ln, font=pf, fill=WHITE)
        point_ls.append(layer(draw))
        y += len(ls) * plh + 80
    head = layer(lambda d: (d.rectangle((76, 408, 90, 450), fill=RED), d.text((108, 400), T["key"], font=BH(52), fill=RED)))
    end = layer(lambda d: (d.text((RW // 2, 820), T["end1"], font=BH(84), fill=WHITE, anchor="mm"),
                           d.text((RW // 2, 930), T["end2"], font=NM(46), fill=SOFT, anchor="mm")))

    # 훅 화면은 제목과 내용이 겹쳐서 뺐다 — 첫 프레임부터 제목이 보이게 (썸네일 겸 멈춰 세우기)
    T_HOOK, T_TITLE, T_POINTS, T_END = 0, 3.0, 8.0, 1.8  # 요약은 읽을 시간 충분히
    table = None
    if a.get("esports") and a.get("gallery"):  # 결과표·대진표 장면 (안전 영역 안에 맞춤)
        tim = fetch_img(a["gallery"][0]["src"])
        if tim:
            r_ = min((SAFE_R - 40) / tim.width, (SAFE_B - SAFE_T) / tim.height)
            tim = tim.resize((int(tim.width * r_), int(tim.height * r_)), Image.LANCZOS)
            table = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
            table.paste(tim, ((SAFE_R + 40 - tim.width) // 2, SAFE_T + (SAFE_B - SAFE_T - tim.height) // 2))
    T_TABLE = 4.0 if table else 0
    total = T_HOOK + T_TITLE + T_TABLE + T_POINTS + T_END
    v = Video(out_path, total, a["id"] + lang)
    for f in range(int(total * FPS)):
        t = f / FPS
        if t < T_HOOK:  # 첫 1.3초: 훅 문구로 멈춰 세우기
            im = Image.composite(dark, zoomed(big, 1.12 - 0.04 * t / T_HOOK), Image.new("L", (RW, RH), 150)).convert("RGBA")
            show(im, hook, 1.0, scale=pop(t / 0.5))  # 첫 프레임부터 보이게 (썸네일)
        elif t < T_HOOK + T_TITLE:
            tt = t - T_HOOK
            im = Image.composite(dark, zoomed(big, 1.0 + 0.08 * tt / T_TITLE), shade).convert("RGBA")
            show(im, label, 1.0)
            for l in title_ls:
                show(im, l, 1.0)
        elif t < T_HOOK + T_TITLE + T_TABLE:
            tt = t - T_HOOK - T_TITLE
            im = Image.new("RGBA", (RW, RH), BG + (255,))
            show(im, table, ease(tt / 0.4))
        elif t < T_HOOK + T_TITLE + T_TABLE + T_POINTS:
            tt = t - T_HOOK - T_TITLE - T_TABLE
            im = blurred.convert("RGBA")
            show(im, head, ease(tt / 0.4))
            for i, l in enumerate(point_ls):
                e = ease((tt - 0.3 - i * 2.2) / 0.5)
                show(im, l, e, (1 - e) * 50)
        else:
            tt = t - T_HOOK - T_TITLE - T_TABLE - T_POINTS
            im = blurred.convert("RGBA")
            e = ease(tt / 0.5)
            show(im, end, e, (1 - e) * 40)
        v.write(im)
    v.close()
    return out_path


def render_deals(deals, out_path, hook_text, key="deals", handle=None):
    """할인 릴스 (골드): 훅 → 게임마다 키아트 + 할인율 + 가격 → 마무리."""
    handle = handle or HANDLE
    dark = Image.new("RGB", (RW, RH), BG)
    games = []
    for x in deals:
        src = fetch_img(x.get("hd") or x["img"]) or Image.new("RGB", (RW, RH), (40, 40, 44))
        big = grade(cover_fit(src, int(RW * 1.15), int(RH * 1.15)), mono=0.2, bright=0.85)
        pct = f"-{x['discount']}%"

        def draw(d, x=x, pct=pct):
            w = d.textlength(pct, font=ANTON(170))
            d.rounded_rectangle((76, 820, 76 + w + 60, 1030), 20, fill=GOLD)  # 안전 영역(아래 캡션 위)으로 올림
            d.text((106, 925), pct, font=ANTON(170), fill=INK, anchor="lm")
            y = 1080
            nf, nls = fit(PROBE, x["name"].replace("™", "").replace("®", ""), BH, 72, SAFE_R - 76, 2)
            for ln in nls:
                d.text((76, y), ln, font=nf, fill=WHITE)
                y += int(nf.size * 1.2)
            o = f"₩{x['initial']:,}"
            d.text((76, y + 50), o, font=NM(46), fill=(150, 150, 158), anchor="lm")
            ow = d.textlength(o, font=NM(46))
            d.line([(76, y + 52), (76 + ow, y + 52)], fill=(150, 150, 158), width=3)
            d.text((76 + ow + 30, y + 50), "→", font=NM(46), fill=(150, 150, 158), anchor="lm")
            d.text((76 + ow + 96, y + 50), f"₩{x['final']:,}", font=BH(96), fill=WHITE, anchor="lm")
        games.append((big, layer(draw)))
    hook = hook_layer(hook_text, GOLD, INK)
    end = layer(lambda d: (d.text((RW // 2, 820), "세일 끝나기 전에", font=BH(84), fill=WHITE, anchor="mm"),
                           d.text((RW // 2, 930), "저장해두세요", font=BH(84), fill=GOLD, anchor="mm")))
    shade = shade_mask(0.3)
    T_HOOK, T_G, T_END = 1.4, 1.7, 1.8
    total = T_HOOK + T_G * len(games) + T_END
    v = Video(out_path, total, key)
    first = games[0][0] if games else dark
    for f in range(int(total * FPS)):
        t = f / FPS
        if t < T_HOOK:
            im = Image.composite(dark, zoomed(first, 1.1), Image.new("L", (RW, RH), 160)).convert("RGBA")
            show(im, hook, 1.0, scale=pop(t / 0.5))  # 첫 프레임부터 보이게 (썸네일)
        elif t < T_HOOK + T_G * len(games):
            k = int((t - T_HOOK) // T_G)
            tt = t - T_HOOK - k * T_G
            big, info = games[k]
            im = Image.composite(dark, zoomed(big, 1.0 + 0.06 * tt / T_G), shade).convert("RGBA")
            e = ease(tt / 0.35)
            show(im, info, e, (1 - e) * 50)
            ImageDraw.Draw(im).text((RW - 70, 150), f"{k + 1}/{len(games)}", font=ANTON(64), fill=GOLD, anchor="rm")
        else:
            tt = t - T_HOOK - T_G * len(games)
            im = Image.composite(dark, zoomed(first, 1.05), Image.new("L", (RW, RH), 200)).convert("RGBA")
            e = ease(tt / 0.5)
            show(im, end, e, (1 - e) * 40)
        v.write(im)
    v.close()
    return out_path


def render_tips(items, out_path, hook_text, key="tips", handle=None):
    """실전 꿀팁 릴스: 훅 → 항목마다 [공식 트레일러 클립 배경 + 날짜 칩 + 큰 이름 + 핵심 2줄] → '저장' 마무리.
    트레일러가 없는 게임은 키아트를 천천히 확대. 위쪽 스토리식 진행 막대로 몇 번째인지 보여 준다."""
    from clips import Clip
    dark = Image.new("RGB", (RW, RH), BG)
    T_HOOK, T_S, T_END = 1.5, 3.2, 1.6
    scenes = []
    for x in items:
        src = fetch_img(x.get("img")) or (x.get("img2") and fetch_img(x["img2"])) or Image.new("RGB", (RW, RH), (40, 40, 44))
        big = grade(cover_fit(src, int(RW * 1.15), int(RH * 1.15)), mono=0.15, bright=0.7)
        clip = None
        if x.get("appid"):
            c = Clip(x["appid"], T_S + (T_HOOK if not scenes else 0))
            clip = c if c.ok else None

        def draw(d, x=x):
            rows = []
            for t in x["lines"][:2]:
                k, _, v = t.partition("  ")
                rows.append((k, v.replace(" (KST)", "")) if v else ("", t))
            # 아래에서부터 쌓는다: 안전 영역 바닥에 맞춰 정렬
            nf, nls = fit(PROBE, x["name"].replace("™", "").replace("®", ""), BH, 118, SAFE_R - 76, 2, 72)
            blocks = []
            for k, v in rows:
                vf, vls = fit(PROBE, v, NB, 54, SAFE_R - 76, 2, 40)
                blocks.append((k, vf, vls))
            h = 92 + 26 + len(nls) * int(nf.size * 1.12) + 30
            h += sum((44 if k else 0) + len(vls) * int(vf.size * 1.3) + 22 for k, vf, vls in blocks)
            y = SAFE_B - h
            cf = BH(56)
            w = d.textlength(x["chip"], font=cf)
            d.rounded_rectangle((76, y, 76 + w + 52, y + 92), 18, fill=RED)
            d.text((102, y + 46), x["chip"], font=cf, fill=WHITE, anchor="lm")
            y += 92 + 26
            for ln in nls:
                d.text((76 + 4, y + 5), ln, font=nf, fill=(0, 0, 0, 150))
                d.text((76, y), ln, font=nf, fill=WHITE)
                y += int(nf.size * 1.12)
            y += 30
            for k, vf, vls in blocks:
                if k:
                    d.text((76, y), k, font=NM(36), fill=(255, 120, 110))
                    y += 44
                for ln in vls:
                    d.text((76 + 3, y + 3), ln, font=vf, fill=(0, 0, 0, 160))
                    d.text((76, y), ln, font=vf, fill=WHITE)
                    y += int(vf.size * 1.3)
                y += 22
        scenes.append((big, clip, layer(draw)))
    n = len(scenes)

    def bars(im, k, p):
        d = ImageDraw.Draw(im)
        gap, x0, x1, y = 10, 60, RW - 60, 110
        w = (x1 - x0 - gap * (n - 1)) / max(1, n)
        for i in range(n):
            a = x0 + i * (w + gap)
            d.rounded_rectangle((a, y, a + w, y + 8), 4, fill=(85, 85, 90))
            f = 1 if i < k else (p if i == k else 0)
            if f > 0:
                d.rounded_rectangle((a, y, a + w * f, y + 8), 4, fill=WHITE)

    hook = hook_layer(hook_text, RED)
    end = layer(lambda d: (d.text((RW // 2, 820), "놓치기 전에", font=BH(96), fill=WHITE, anchor="mm"),
                           d.text((RW // 2, 950), "저장해두세요", font=BH(96), fill=RED, anchor="mm")))
    shade = shade_mask(0.38, 240)
    top = Image.new("L", (1, RH))
    for yy in range(RH):
        top.putpixel((0, yy), int(max(0, 1 - yy / 360) * 150))
    top = top.resize((RW, RH))
    total = T_HOOK + T_S * n + T_END
    v = Video(out_path, total, key)
    gens = [c.frames() if c else None for _, c, _ in scenes]
    still_t = [0] * n
    last = dark
    for f in range(int(total * FPS)):
        t = f / FPS
        if t < T_HOOK + T_S * n:
            k = 0 if t < T_HOOK else int((t - T_HOOK) // T_S)
            k = min(k, n - 1)
            big, clip, info = scenes[k]
            if gens[k]:
                bg = next(gens[k])
            else:
                still_t[k] += 1
                bg = zoomed(big, 1.0 + 0.08 * still_t[k] / (FPS * (T_S + T_HOOK)))
            last = bg
            if t < T_HOOK:
                im = Image.composite(dark, bg, Image.new("L", (RW, RH), 130)).convert("RGBA")
                show(im, hook, 1.0, scale=pop(t / 0.5))  # 첫 프레임부터 보이게 (썸네일)
            else:
                tt = t - T_HOOK - k * T_S
                im = Image.blend(bg, dark, 0.3 if gens[k] else 0.45)  # 글자가 잘 읽히게 전체를 한 톤 낮추고 (정지 키아트는 더)
                im = Image.composite(dark, im, shade)
                im = Image.composite(dark, im, top).convert("RGBA")
                e = ease(tt / 0.3)
                show(im, info, e, (1 - e) * 60)
                bars(im, k, tt / T_S)
        else:
            tt = t - T_HOOK - T_S * n
            im = Image.composite(dark, last, Image.new("L", (RW, RH), 190)).convert("RGBA")
            e = ease(tt / 0.4)
            show(im, end, e, (1 - e) * 40)
        v.write(im)
    v.close()
    for _, c, _ in scenes:
        if c:
            c.close()
    return out_path


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    arts ={a["id"]: a for a in json.loads((ROOT / "data/articles.json").read_text(encoding="utf-8"))}
    out = ROOT / "out" / "reels"
    out.mkdir(parents=True, exist_ok=True)
    for i in sys.argv[1:] or ["5e9b636987"]:
        p = render(arts[i], out / f"{i}.mp4")
        print(p, f"{p.stat().st_size / 1e6:.1f}MB")
    if len(sys.argv) == 1:
        import deals_carousel as dc
        p = render_deals(dc.pick_deals(5), out / "deals.mp4", "이번 주 역대 최저가 5")
        print(p, f"{p.stat().st_size / 1e6:.1f}MB")
