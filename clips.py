"""릴스 배경용 짧은 영상 클립: 스팀 공식 트레일러에서 컷이 촘촘한(화려한) 몇 초를 골라 세로 1080x1920 프레임으로.

  c = Clip(appid, seconds, article=None)   # 준비(다운로드·구간 선택). 실패하면 c.ok == False
  for im in c.frames(): ...                # PIL RGB 프레임 (FPS 24), 다 쓰면 c.close()
"""
import re, subprocess, tempfile
from pathlib import Path
from PIL import Image

from reels import ffmpeg_bin, RW, RH, FPS
from trailer_reel import highlight_start, match_movie
from video_carousel import steam_movies

BAD = re.compile(r"teaser|announce|logo|rating|esrb|cinematic|story trailer", re.I)


def pick(movies, article=None):
    ok = [m for m in movies if m.get("hls_h264") or (m.get("mp4") or {}).get("max")]
    if article:
        m = match_movie(article, ok)
        if m:
            return m
    good = [m for m in ok if not BAD.search(m["name"])] or ok
    pref = [m for m in good if re.search(r"gameplay|launch|release|overview|feature", m["name"], re.I)] or good
    return max(pref, key=lambda m: m.get("id", 0)) if pref else None


class Clip:
    def __init__(self, appid, seconds, article=None, dim=0.72):
        self.ok, self.proc, self.seconds, self.dim = False, None, seconds, dim
        try:
            mv = pick(steam_movies(appid), article)
            if not mv:
                return
            src = mv.get("hls_h264") or mv["mp4"]["max"]
            self.raw = Path(tempfile.mkdtemp()) / "c.mp4"
            subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", src, "-t", "90", "-an", "-c", "copy", str(self.raw)],
                           check=True, timeout=240)
            probe = subprocess.run([ffmpeg_bin(), "-i", str(self.raw)], capture_output=True, text=True).stderr
            h, m, s = probe.split("Duration: ")[1].split(",")[0].split(":")
            dur = int(h) * 3600 + int(m) * 60 + float(s)
            if dur < seconds + 2:
                return
            # 앞 로고·끝 타이틀을 피해 컷이 가장 촘촘한 구간 (최소 3초 지점부터)
            # 트레일러 끝 1/4 은 보통 타이틀·출시일 글자 화면이라 쓰지 않는다
            self.start = max(3.0, highlight_start(self.raw, dur * 0.75 + 4, win=seconds, tail_skip=4))
            self.name, self.ok = mv["name"], True
        except Exception as e:
            print(f"  ! 클립 실패 {appid}: {e}")

    def frames(self):
        vf = (f"scale={RW}:{RH}:force_original_aspect_ratio=increase,crop={RW}:{RH},fps={FPS},"
              f"eq=brightness={-(1 - self.dim) * 0.5:.2f}:saturation=1.05")
        self.proc = subprocess.Popen([ffmpeg_bin(), "-loglevel", "error", "-ss", f"{self.start:.2f}", "-i", str(self.raw),
                                      "-t", f"{self.seconds + 0.5:.2f}", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                     stdout=subprocess.PIPE)
        size = RW * RH * 3
        last = None
        while True:
            buf = self.proc.stdout.read(size)
            if len(buf) < size:
                break
            last = Image.frombytes("RGB", (RW, RH), buf)
            yield last
        while last is not None:  # 클립이 모자라면 마지막 프레임 유지
            yield last

    def close(self):
        if self.proc:
            self.proc.kill()
        try:
            self.raw.unlink(missing_ok=True)
        except Exception:
            pass
