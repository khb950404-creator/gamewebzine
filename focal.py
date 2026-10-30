"""FocalNet 사람 선호 크롭 (appwrite/focalnet, MIT) — CPU ONNX 추론만 옮긴 최소 버전.

중요도 지도(64x64)로 피사체를 지키는 후보만 남기고, 사람이 고른 구도 데이터로 학습한 점수가 가장 높은 크롭을 고른다.
  best_crop(PIL 이미지, 가로/세로 비율) -> (left, top, width, height) 원본 픽셀 좌표, 실패하면 None
"""
from pathlib import Path

import numpy as np
from PIL import Image

MODEL = Path(__file__).parent / "models" / "focalnet-human.onnx"
SIZE, MAP, MAXC = 256, 64, 128
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_S = []


def _session():
    if not _S:
        import onnxruntime as ort
        o = ort.SessionOptions()
        o.intra_op_num_threads = 2
        _S.append(ort.InferenceSession(str(MODEL), o, providers=["CPUExecutionProvider"]))
    return _S[0]


def _prepare(img):
    s = min(SIZE / img.width, SIZE / img.height)
    w, h = max(1, min(SIZE, int(img.width * s + .5))), max(1, min(SIZE, int(img.height * s + .5)))
    left, top = (SIZE - w) // 2, (SIZE - h) // 2
    px = (np.asarray(img.convert("RGB").resize((w, h), Image.BILINEAR), dtype=np.float32) / 255 - MEAN) / STD
    t = np.zeros((1, 3, SIZE, SIZE), dtype=np.float32)
    t[0, :, top:top + h, left:left + w] = px.transpose(2, 0, 1)
    return t, (left, top, w, h)


def _candidates(iw, ih, ratio, positions=5, scales=(1.0, 0.9, 0.8, 0.7, 0.65)):
    src = iw / ih
    mw, mh = (ratio / src, 1.0) if src > ratio else (1.0, src / ratio)
    out = []
    for sc in scales:
        w, h = mw * sc, mh * sc
        for t in np.linspace(0, 1 - h, positions if h < 1 - 1e-7 else 1):
            for l in np.linspace(0, 1 - w, positions if w < 1 - 1e-7 else 1):
                out.append((l, t, l + w, t + h))
    return np.unique(np.round(np.asarray(out, dtype=np.float32), 7), axis=0)


def _restore(m, box):
    """패딩을 걷어낸 원본 비율 중요도 지도 (최근접 샘플링으로 충분)."""
    l, t, w, h = box
    g = m.shape[0]
    xs = np.clip(((l + (np.arange(MAP) + .5) / MAP * w) / SIZE * g).astype(int), 0, g - 1)
    ys = np.clip(((t + (np.arange(MAP) + .5) / MAP * h) / SIZE * g).astype(int), 0, g - 1)
    return m[np.ix_(ys, xs)]


def _retain(hm, b):
    """정규화 박스(l,t,r,b)가 담는 중요도 비율."""
    tot = hm.sum()
    if tot <= 0:
        return 0.0
    e = np.linspace(0, 1, MAP + 1)
    wx = np.clip(np.minimum(e[1:], b[2]) - np.maximum(e[:-1], b[0]), 0, None) * MAP
    wy = np.clip(np.minimum(e[1:], b[3]) - np.maximum(e[:-1], b[1]), 0, None) * MAP
    return float(wy @ hm @ wx / tot)


def best_crop(img, ratio, tolerance=0.05, min_scale=0.0):
    """min_scale: 너무 확대된 후보 제외 (1.0 이면 최대 크기 크롭만 = 위치만 고른다)."""
    try:
        t, box = _prepare(img)
        cands = _candidates(img.width, img.height, ratio)
        full = cands[:, 2] - cands[:, 0]
        cands = cands[full >= full.max() * min_scale - 1e-6]
        boxes = np.zeros((1, MAXC, 4), dtype=np.float32)
        boxes[0, :len(cands)] = cands
        l, tp, w, h = box
        content = np.asarray([[l, tp, l + w, tp + h]], dtype=np.float32) / SIZE
        imp, sc = _session().run(["importance", "crop_scores"], {"image": t, "boxes": boxes, "content": content})
        hm = _restore(np.clip(imp[0, 0], 0, 1), box)
        ret = np.asarray([_retain(hm, b) for b in cands])
        ok = np.flatnonzero(ret >= ret.max() - tolerance)
        b = cands[ok[np.argmax(sc[0, ok])]]
        x0, y0 = int(b[0] * img.width + .5), int(b[1] * img.height + .5)
        return x0, y0, max(1, int(b[2] * img.width + .5) - x0), max(1, int(b[3] * img.height + .5) - y0)
    except Exception:
        return None
