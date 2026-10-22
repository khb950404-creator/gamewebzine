"""영상 캐러셀: 표지(키아트 + 두 줄 제목) → 장마다 [위: 공식 트레일러 몇 초 / 아래: 소제목 + 짧은 설명].

영상은 Steam 상점에 게임사가 올린 공식 트레일러만 쓴다 (유튜브 다운로드 안 함). 소리는 뺀다.

  python video_carousel.py <기사ID>   # out/vcar/<ID>/ 에 01.jpg, 02.mp4 ... 생성
"""
import json, re, subprocess, sys, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw

from cards import W, H
from card_themes import BH, NB, NM, fit, HANDLE, shade, bg
from carousel import LIME, WHITE, SOFT, best_image, safe
from reels import ffmpeg_bin

VW, VH = W, int(W * 9 / 16)   # 위쪽 영상 칸 1080x608
VY = 150                       # 영상 칸 위치
CLIP = 7                       # 장마다 7초
INK = (8, 8, 10)

SLIDES_PROMPT = """너는 게임 매체 인스타 카드뉴스 편집자다. 아래 기사를 영상 캐러셀 3~4장으로 나눠라.
각 장: "head" = 그 장의 핵심 한 줄 (12~22자, 구체적으로. 예: "쌍권총은 '냉정'에서 해금"),
"lines" = 그 장을 설명하는 짧은 줄 2~4개 (각 18~34자, 개조식 '~함/~됨/~가능' 또는 명사형. 숫자·이름 살리기).
장끼리 내용이 겹치지 않게, 기사에 있는 사실만.
"cover" = 표지 두 줄 제목 [윗줄(맥락, 8~16자), 아랫줄(핵심, 8~16자)] 예: ["엣지러너 2 기념 업데이트", "V도 드디어 '쌍권총'"]
JSON 하나만: {{"cover": [..,..], "slides": [{{"head": .., "lines": [..]}}]}}

기사: {art}"""


def steam_movies(appid):
    j = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=movies&l=koreana",
        headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
    return (j.get(str(appid), {}).get("data") or {}).get("movies") or []


def pick_movie(a, movies):
    """기사 제목과 이름이 가장 잘 맞는 트레일러 (게임플레이 > 나머지, CGI·광고성 뒤로)."""
    words = set(re.findall(r"[a-z0-9]+", (a.get("title_en") or "").lower()))
    ko = a.get("title", "")
    def score(m):
        n = m["name"].lower()
        s = sum(1 for w in words if len(w) > 3 and w in n) * 3
        s += 4 if ("게임플레이" in ko or "gameplay" in words) and "gameplay" in n else 0
        s += 4 if ("업데이트" in ko or "패치" in ko) and ("update" in n or "patch" in n) else 0
        s += 2 if "gameplay" in n else 0
        s -= 3 if "cgi" in n or "launch" in n and "출시" not in ko else 0
        return s
    movies = [m for m in movies if m.get("hls_h264") or (m.get("mp4") or {}).get("max")]
    return max(movies, key=score) if movies else None


def make_slides_text(a):
    import webzine
    art = {k: a.get(k) for k in ("title", "tldr", "body")}
    r = webzine.llm(SLIDES_PROMPT.format(art=json.dumps(art, ensure_ascii=False)))
    return r


def cover(a, lines):
    img = shade(bg({**a, "img_hd": best_image(a)}, mono=0.0, bright=0.85), 0.30, 235)  # 표지는 컬러
    d = ImageDraw.Draw(img)
    f1, l1 = fit(d, safe(lines[0]), BH, 92, W - 152, 1, 60)
    f2, l2 = fit(d, safe(lines[1]), BH, 108, W - 152, 1, 72)  # 핵심 줄은 가능하면 한 줄로
    if len(l2) > 1:
        f2, l2 = fit(d, safe(lines[1]), BH, 96, W - 152, 2, 64)
    y = H - 210 - len(l2) * int(f2.size * 1.15)
    d.text((76, y - int(f1.size * 1.25)), l1[0], font=f1, fill=WHITE)
    for ln in l2:
        d.text((76, y), ln, font=f2, fill=WHITE)
        y += int(f2.size * 1.15)
    d.text((W - 56, H - 70), HANDLE, font=NB(32), fill=LIME, anchor="ra")
    return img


def text_panel(head, lines, page, total):
    """영상 아래 글 영역 (영상 칸은 비워 둔다 — ffmpeg 가 영상을 얹는다)."""
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    d.text((W - 56, 70), f"{page}/{total}", font=NB(30), fill=LIME, anchor="ra")
    y = VY + VH + 70
    hf, hl = fit(d, safe(head), BH, 64, W - 152, 2, 44)
    for ln in hl:
        d.text((76, y), ln, font=hf, fill=WHITE)
        y += int(hf.size * 1.25)
    y += 24
    for t in lines[:4]:
        tf, tl = fit(d, t, NM, 40, W - 152, 2, 30)
        for ln in tl:
            d.text((76, y), ln, font=tf, fill=SOFT)
            y += int(tf.size * 1.5)
        y += 6
    d.text((W - 56, H - 70), HANDLE, font=NB(32), fill=LIME, anchor="ra")
    return img


def fetch_trailer(src, out, seconds=60):
    """HLS 트레일러 앞부분을 내려받아 로컬 파일로 (HLS 를 직접 자르면 앞부분이 비는 경우가 있다)."""
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", src, "-t", str(seconds), "-an", "-c:v", "copy",
                    str(out)], check=True, timeout=300)
    return out


def render_clip(src, start, panel_png, out):
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", str(start), "-t", str(CLIP), "-i", src,
           "-loop", "1", "-i", str(panel_png),
           "-filter_complex", f"[0:v]scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH},setsar=1,fps=30[v];"
                              f"[1:v][v]overlay=0:{VY}:shortest=1,format=yuv420p[o]",
           "-map", "[o]", "-an", "-t", str(CLIP), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-profile:v", "high", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=300)


def build(a, out_dir, texts=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mv = pick_movie(a, steam_movies(a["appid"])) if a.get("appid") else None
    if not mv:
        raise RuntimeError("스팀 공식 트레일러 없음")
    src = fetch_trailer(mv.get("hls_h264") or mv["mp4"]["max"], out / "_trailer.mp4")
    texts = texts or make_slides_text(a)
    slides = texts["slides"][:4]
    total = len(slides) + 1
    cover(a, texts["cover"]).convert("RGB").save(out / "01.jpg", quality=90)
    files = ["01.jpg"]
    for i, s in enumerate(slides):
        png = out / f"_p{i}.png"
        text_panel(s["head"], s["lines"], i + 2, total).save(png)
        name = f"{i + 2:02d}.mp4"
        render_clip(src, 4 + i * (CLIP + 3), png, out / name)  # 장마다 트레일러 다른 구간
        png.unlink()
        files.append(name)
    src.unlink(missing_ok=True)
    print(f"영상 캐러셀: {mv['name']} → {files}")
    return files, texts


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import webzine
    webzine.load_env()
    a = next(x for x in webzine.load_json(webzine.DATA / "articles.json", []) if x["id"] == sys.argv[1])
    build(a, Path("out/vcar") / a["id"])
