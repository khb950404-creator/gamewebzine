"""릴스 배경음.

기본: media/bgm/ 의 실제 곡 (Kevin MacLeod, incompetech.com, CC BY 4.0 — 캡션에 credit() 표기 필수).
  곡마다 가장 에너지 높은 50초 구간만 잘라 둔 것. 게시물 id 로 곡을 골라 게시할 때도 같은 곡을 표기할 수 있게 한다.
곡이 없으면: 직접 합성 앰비언트 (아래 pad/arp).

신스 패드(코드) + 아주 작은 아르페지오. 코드 진행 3종을 기사마다 번갈아 쓴다.
  python bgm.py   # out/reels/bgm_*.wav 샘플
"""
import hashlib, wave
from pathlib import Path
import numpy as np

SR = 44100
# 반음 단위 (A4=440 기준 MIDI 번호)
PROGRESSIONS = [
    [(57, 60, 64, 67), (53, 57, 60, 64), (48, 55, 60, 64), (55, 59, 62, 67)],  # Am7 - Fmaj7 - Cadd9 - G
    [(50, 57, 60, 65), (46, 53, 58, 62), (48, 55, 60, 64), (45, 52, 57, 60)],  # Dm9 - Bbmaj7 - C - Am
    [(52, 59, 62, 67), (48, 55, 59, 64), (45, 52, 55, 60), (47, 54, 59, 62)],  # Em7 - Cmaj7 - Am7 - Bm
]


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    # 한 극점 저역 필터 (부드럽게 깎기) — 벡터화가 어려워 블록 단위로
    for i in range(0, len(x), 4096):
        seg = x[i:i + 4096]
        out = np.empty_like(seg)
        for j, v in enumerate(seg):
            acc = (1 - a) * v + a * acc
            out[j] = acc
        y[i:i + 4096] = out
    return y


def pad(seconds, prog):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    seg = seconds / len(prog)
    for k, chord in enumerate(prog):
        start, end = k * seg, (k + 1) * seg + 0.9  # 다음 코드와 살짝 겹침
        env = np.clip((t - start) / 0.9, 0, 1) * np.clip((end - t) / 0.9, 0, 1)
        env = env ** 1.5
        for m in chord:
            f = hz(m)
            for det in (-0.12, 0.0, 0.12):  # 살짝 어긋난 3개 → 넓고 부드러운 패드
                ph = 2 * np.pi * f * (2 ** (det / 12)) * t
                out += env * (np.sin(ph) + 0.18 * np.sin(2 * ph) + 0.06 * np.sin(3 * ph))
    return out


def arp(seconds, prog, rng):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    step = 0.36  # 8분음표 느낌
    seg = seconds / len(prog)
    k = 0
    while k * step < seconds - 0.5:
        st = k * step
        chord = prog[min(int(st // seg), len(prog) - 1)]
        m = chord[[0, 2, 3, 1, 2, 3, 1, 2][k % 8]] + 12
        f = hz(m)
        idx = (t >= st)
        tt = t[idx] - st
        out[idx] += np.exp(-tt * 5.5) * np.sin(2 * np.pi * f * tt) * (0.7 + 0.3 * rng.random())
        k += 1
    return out


TRACKS = Path(__file__).parent / "media" / "bgm"


def track(key):
    fs = sorted(TRACKS.glob("*.mp3"))
    return fs[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(fs)] if fs else None


def credit(key):
    t = track(key)
    return f'음악: "{t.stem.replace("_", " ")}" Kevin MacLeod (incompetech.com) · CC BY 4.0' if t else ""


def make(path, seconds=11.5, key=""):
    t = track(key)
    if t:
        import subprocess
        from reels import ffmpeg_bin
        fo = max(0.0, seconds - 1.2)
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(t), "-t", f"{seconds:.2f}",
                        "-af", f"loudnorm=I=-18:TP=-2,afade=t=out:st={fo:.2f}:d=1.2", "-ar", str(SR), "-ac", "2", str(path)],
                       check=True, timeout=120)
        return path
    return synth(path, seconds, key)


def synth(path, seconds=11.5, key=""):
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    prog = PROGRESSIONS[h % len(PROGRESSIONS)]
    rng = np.random.default_rng(h % 2**32)
    x = pad(seconds, prog) * 0.5 + arp(seconds, prog, rng) * 0.35
    x = lowpass(x, 2200)
    n = len(x)
    fade = np.ones(n)
    fi, fo = int(0.8 * SR), int(1.4 * SR)
    fade[:fi] = np.linspace(0, 1, fi) ** 2
    fade[-fo:] = np.linspace(1, 0, fo) ** 2
    x *= fade
    x = x / (np.max(np.abs(x)) + 1e-9) * 10 ** (-20 / 20)  # 아주 잔잔하게: 최고 -20dBFS
    # 스테레오: 아르페지오 쪽만 살짝 좌우 차이
    left = x
    right = np.concatenate([np.zeros(int(0.012 * SR)), x[:-int(0.012 * SR)]])
    pcm = (np.stack([left, right], 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


if __name__ == "__main__":
    out = Path(__file__).parent / "out" / "reels"
    out.mkdir(parents=True, exist_ok=True)
    for i, k in enumerate(["a", "b", "c"]):
        print(make(out / f"bgm_{i}.wav", key=k))
