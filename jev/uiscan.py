"""Template checks for game UI panels that change what input does: the shop (its tab bar at the
top, or its SELL / UNDO buttons; a tooltip can cover either one) and modal dialogs. About 1 ms."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

_ASSETS = Path(__file__).parent / "assets"
_cache: dict[str, np.ndarray | None] = {}


def _tpl(name: str) -> np.ndarray | None:
    if name not in _cache:
        t = cv2.imread(str(_ASSETS / name))
        _cache[name] = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY) if t is not None else None
    return _cache[name]


def _gray(box: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(box, cv2.COLOR_BGRA2GRAY if box.ndim == 3 and box.shape[2] == 4 else cv2.COLOR_BGR2GRAY)


def _score(frame: np.ndarray, name: str, y0: int, y1: int, x0: int, x1: int) -> float:
    t = _tpl(name)
    if t is None:
        return 0.0
    return float(cv2.matchTemplate(_gray(frame[y0:y1, x0:x1]), t, cv2.TM_CCOEFF_NORMED).max())


def shop_visible(frame: np.ndarray) -> bool:
    return _score(frame, "shop_tabs.png", 140, 240, 340, 760) > 0.8 or _score(frame, "shop_sell_undo.png", 790, 880, 360, 640) > 0.8


SHOP_CORNER_AT = (1316, 166)  # where the shop's top-right corner template sits in its normal place


def shop_corner(frame: np.ndarray) -> tuple[int, int, float]:
    """Where the shop's top-right corner (gold border and close X) is on screen, anywhere, with the
    match score. The panel can be dragged: in g16 it sat 1068 px left of its place, every search
    click missed, and nothing could be bought for the rest of the game. ~15 ms."""
    t = _tpl("shop_corner.png")
    if t is None:
        return (0, 0, 0.0)
    y0 = 60
    r = cv2.matchTemplate(_gray(frame[y0:1000, :]), t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    return (int(loc[0]), int(loc[1]) + y0, float(mx))
