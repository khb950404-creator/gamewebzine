"""트레일러 릴스: 게임사 공식 트레일러(스팀 상점 영상)를 소리 그대로, 세로 화면으로 간단히 편집.

  화면: 흐린 트레일러 배경 + 가운데 원본 16:9 영상, 위에 제목, 아래 핵심 정보 2~3줄, 출처 표기
  길이: 최대 MAX_SEC 초 — 컷이 가장 촘촘한 구간(하이라이트), 끝에서 소리·화면 페이드아웃

  python trailer_reel.py <기사ID>   # out/trailer/<ID>.mp4
"""
import json, re, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw

from card_themes import BH, NB, NM, fit
from carousel import LIME as RED, WHITE, SOFT, safe
from reels import ffmpeg_bin, RW, RH, SAFE_R, SAFE_B, SAFE_T
from video_carousel import steam_movies

MAX_SEC = 45
VY = 600          # 영상 칸 위쪽 (1080x608)
VH = 608
PROBE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


GENERIC = set("trailer official update launch release gameplay teaser announce announcement reveal video the a of and "
              "new season story cinematic edition dlc expansion part pack free now available game patch".split())


def match_movie(a, movies):
    """기사 내용과 맞는 트레일러만: 기사 영문 제목·검색어의 고유 단어(게임명 제외)가 영상 이름에 있어야 한다.
    출시 기사면 런치/출시 트레일러도 인정. 못 찾으면 None (엉뚱한 옛 트레일러를 쓰지 않는다)."""
    game = set(re.findall(r"[a-z0-9]+", " ".join(a.get("games_en") or []).lower()))
    words = set(re.findall(r"[a-z0-9]+", ((a.get("title_en") or "") + " " + (a.get("video_q") or "")).lower()))
    key = {w for w in words - game - GENERIC if len(w) > 2 and not w.isdigit()}
    movies = [m for m in movies if m.get("hls_h264") or (m.get("mp4") or {}).get("max")]
    hit = [m for m in movies if key & set(re.findall(r"[a-z0-9]+", m["name"].lower()))]
    if not hit and re.search(r"출시|발매|정식", a.get("title", "")) and not re.search(r"DLC|확장|업데이트|시즌|패치|모드|판", a.get("title", "")):
        hit = [m for m in movies if re.search(r"launch|release", m["name"], re.I)]
    return max(hit, key=lambda m: m.get("id", 0)) if hit else None  # 같은 조건이면 최신 영상


def highlight_start(raw, dur):
    """장면 전환(컷)이 가장 촘촘한 MAX_SEC 구간의 시작점. 앞쪽 로고·검은 화면과 끝 타이틀 카드를 건너뛴다.
    (PySceneDetect 와 같은 방식을 ffmpeg 내장 scene 점수로) 짧은 영상이거나 실패하면 0."""
    if dur <= MAX_SEC + 3:
        return 0.0
    try:
        log = subprocess.run([ffmpeg_bin(), "-i", str(raw), "-an", "-vf", "scale=320:-2,select='gt(scene,0.3)',showinfo",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=300).stderr
        cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", log)]
    except Exception:
        return 0.0
    if len(cuts) < 4:
        return 0.0
    tail = dur - MAX_SEC - 3  # 끝 3초(로고·출시일 카드)는 남겨 두지 않는다
    starts = [0.0] + [c for c in cuts if c <= tail]
    best = max(starts, key=lambda t: (sum(t <= c < t + MAX_SEC for c in cuts), -t))
    return max(0.0, best - 0.05)


def info_lines(a):
    """영상 아래 알맹이: 체크 포인트 값(날짜·가격·플랫폼) 우선, 없으면 핵심 요약."""
    rows = [f"{c['k']}  {c['v']}" for c in (a.get("check") or [])][:2]
    return rows or [t.rstrip(".") for t in (a.get("tldr") or [])][:2]


def panel(a, credit):
    img = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 제목: 영상 위 (안전 영역 안)
    tf, tl = fit(d, safe(a.get("subject") or a["title"]), BH, 70, SAFE_R - 76, 3, 46)
    lh = int(tf.size * 1.22)
    y = VY - 40 - len(tl) * lh
    d.text((76, y - 56), "공식 트레일러", font=NB(34), fill=RED)
    for ln in tl:
        d.text((76 + 3, y + 4), ln, font=tf, fill=(0, 0, 0, 160))
        d.text((76, y), ln, font=tf, fill=WHITE)
        y += lh
    # 정보: 영상 아래
    d.text((RW - 40, VY + VH + 14), f"영상: {credit}", font=NM(24), fill=(170, 170, 178), anchor="ra")
    y = VY + VH + 80
    for t in info_lines(a):
        f, ls = fit(d, t, NM, 40, SAFE_R - 76, 2, 30)
        for ln in ls:
            if y > SAFE_B - 60:
                break
            d.text((76, y), ln, font=f, fill=SOFT)
            y += int(f.size * 1.45)
        y += 8
    return img


def build(a, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    mv = match_movie(a, steam_movies(a["appid"])) if a.get("appid") else None
    if not mv:
        raise RuntimeError("기사와 맞는 스팀 공식 트레일러 없음")
    src = mv.get("hls_h264") or (mv.get("mp4") or {}).get("max")
    raw = out.with_suffix(".src.mp4")
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", src, "-t", "150", "-c", "copy", str(raw)],
                   check=True, timeout=300)
    probe = subprocess.run([ffmpeg_bin(), "-i", str(raw)], capture_output=True, text=True).stderr
    h, m, sec = probe.split("Duration: ")[1].split(",")[0].split(":")
    full = int(h) * 3600 + int(m) * 60 + float(sec)
    ss = highlight_start(raw, full)
    dur = min(full - ss, MAX_SEC)
    credit = ", ".join((a.get("publishers") or [])[:1]) or (a.get("games") or [""])[0]
    credit = f"{credit} 공식 트레일러".strip()
    png = out.with_suffix(".panel.png")
    panel(a, credit).save(png)
    has_audio = "Audio:" in probe
    fo = max(0.0, dur - 1.5)
    vf = (f"[0:v]split[a][b];[a]scale={RW}:{RH}:force_original_aspect_ratio=increase,crop={RW}:{RH},"
          f"boxblur=24:2,eq=brightness=-0.32:saturation=0.8[bg];[b]scale={RW}:{VH}:force_original_aspect_ratio=decrease,"
          f"pad={RW}:{VH}:(ow-iw)/2:(oh-ih)/2[fg];[bg][fg]overlay=0:{VY}[v1];[v1][1:v]overlay=0:0,"
          f"fade=t=out:st={fo}:d=1.5,fps=30,setsar=1,format=yuv420p[o]")
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", f"{ss:.2f}", "-i", str(raw), "-loop", "1", "-i", str(png),
           "-filter_complex", vf + (f";[0:a]afade=t=in:d={0.4 if ss else 0},afade=t=out:st={fo}:d=1.5,aresample=44100[ao]" if has_audio else ""),
           "-map", "[o]"] + (["-map", "[ao]", "-c:a", "aac", "-b:a", "160k"] if has_audio else []) + [
           "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high",
           "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=600)
    raw.unlink(missing_ok=True)
    png.unlink(missing_ok=True)
    print(f"트레일러 릴스: {mv['name']} ({ss:.0f}초부터 {dur:.0f}초, 소리 {'있음' if has_audio else '없음'})")
    return mv["name"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    arts = {a["id"]: a for a in json.loads(Path("data/articles.json").read_text(encoding="utf-8"))}
    a = arts[sys.argv[1]]
    build(a, Path("out/trailer") / f"{a['id']}.mp4")
