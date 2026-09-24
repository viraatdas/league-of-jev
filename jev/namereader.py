"""Who is that champion: the name printed above an enemy health bar, read with the macOS Vision
text recognizer and matched to the enemy team's champion names. Bots are named after their
champion, so the label is the champion. Once per new champion track (~10-30 ms), then cached.
"""
from __future__ import annotations

import difflib

import cv2
import numpy as np

try:
    import Quartz
    import Vision
    from Foundation import NSData
except Exception:  # noqa: BLE001  not on macOS / bindings missing: no names, the planner falls back
    Vision = None


def available() -> bool:
    return Vision is not None


def read_text(bgr: np.ndarray, fast: bool = False) -> list[str]:
    """Text lines in a small BGR image, most confident first."""
    if Vision is None or bgr is None or bgr.size == 0:
        return []
    ok, png = cv2.imencode(".png", bgr)
    if not ok:
        return []
    data = NSData.dataWithBytes_length_(png.tobytes(), len(png))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None:
        return []
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(1 if fast else 0)   # 0 accurate, 1 fast
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if cands:
            out.append(str(cands[0].string()))
    return out


def name_crop(frame: np.ndarray, bar: tuple[int, int, int, int]) -> np.ndarray:
    """The label above a champion bar: centred on the bar, 4-26 px above its fill, upscaled."""
    bx, by, bw, bh = bar
    x0, x1 = max(0, bx - 60), min(frame.shape[1], bx + bw + 110)
    y0, y1 = max(0, by - 27), max(0, by - 3)
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return crop
    if crop.shape[2] == 4:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
    return cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)


def match(texts: list[str], names: list[str]) -> str | None:
    """The enemy champion whose name the label spells (fuzzy: 'Nasus' read as 'Nasu5' still matches)."""
    best, score = None, 0.0
    for t in texts:
        t = t.strip().lower()
        if len(t) < 3:
            continue
        for n in names:
            r = difflib.SequenceMatcher(None, t, n.lower()).ratio()
            if n.lower() in t:
                r = max(r, 0.95)
            if r > score:
                best, score = n, r
    return best if score >= 0.7 else None


def identify(frame: np.ndarray, bar: tuple[int, int, int, int], names: list[str]) -> str | None:
    return match(read_text(name_crop(frame, bar)), names)
