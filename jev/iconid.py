"""Which enemy champion is which icon on the minimap.

The minimap reader finds enemy champion icons (red rings) but not who they are. Each icon's portrait is
the champion's square Data Dragon art cut to a circle, ~24 px across on our minimap. Matching the
inner square of that circle (clear of the ring) against the five enemy champions' art, a few pixels
around the detected centre and at three sizes, names the icon; each champion is used once per frame
and a weak or ambiguous match stays unnamed.

On g35's saved frames the matches landed where those champions play: Nasus top, Ezreal and Taric bot,
Anivia mid, Xin Zhao in the jungle (0.4-0.9 for the confident ones).
"""
from __future__ import annotations

import json

import cv2
import httpx
import numpy as np

from jev.items import CACHE

DDRAGON = "https://ddragon.leagueoflegends.com"
SIZES = (16, 17, 18)       # inner-square template side (px) for portraits ~22-26 px across
SEARCH = 4                 # px around the detected centre
MIN_SCORE, MIN_MARGIN = 0.35, 0.08


def _version() -> str | None:
    cached = sorted(CACHE.glob("*/champion.json"))
    return cached[-1].parent.name if cached else None


def champion_art(name: str) -> np.ndarray | None:
    """The champion's square art (BGR), from the Data Dragon cache, fetched once when missing."""
    v = _version()
    if v is None:
        return None
    try:
        summary = json.loads((CACHE / v / "champion.json").read_text())["data"]
        key = name.lower().replace(" ", "").replace("'", "").replace(".", "")
        cid = next(k for k, d in summary.items()
                   if k.lower() == key or d["name"].lower().replace(" ", "").replace("'", "").replace(".", "") == key)
        f = CACHE / v / "img" / f"{cid}.png"
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            r = httpx.get(f"{DDRAGON}/cdn/{v}/img/champion/{cid}.png", timeout=8)
            r.raise_for_status()
            f.write_bytes(r.content)
        return cv2.imread(str(f))
    except Exception:  # noqa: BLE001  offline and not cached, or an unknown name
        return None


class IconMatcher:
    def __init__(self, names: list[str]) -> None:
        self.names: list[str] = []
        self.tpl: dict[str, list[np.ndarray]] = {}
        for n in names:
            art = champion_art(n)
            if art is None:
                continue
            h, w = art.shape[:2]
            # The circle is inscribed in the square art; its inner square is the middle 1/sqrt(2).
            m = int(round(h * (1 - 1 / np.sqrt(2)) / 2))
            inner = art[m:h - m, m:w - m]
            self.tpl[n] = [cv2.resize(inner, (s, s), interpolation=cv2.INTER_AREA) for s in SIZES]
            self.names.append(n)

    def scores(self, crop_bgr: np.ndarray, centre: tuple[float, float]) -> dict[str, float]:
        """Best normalised correlation of each champion's art around one icon centre (minimap px)."""
        cx, cy = int(round(centre[0])), int(round(centre[1]))
        out: dict[str, float] = {}
        for n in self.names:
            best = -1.0
            for t in self.tpl[n]:
                s = t.shape[0]
                r = s // 2 + SEARCH
                y0, x0 = cy - r, cx - r
                if y0 < 0 or x0 < 0 or y0 + 2 * r + 1 > crop_bgr.shape[0] or x0 + 2 * r + 1 > crop_bgr.shape[1]:
                    continue
                win = crop_bgr[y0:y0 + 2 * r + 1, x0:x0 + 2 * r + 1]
                res = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED)
                best = max(best, float(res.max()))
            out[n] = best
        return out

    def identify(self, crop_bgr: np.ndarray, centres: list[tuple[float, float]]) -> list[str | None]:
        """A name (or None) for each icon centre: the strongest pairs first, each champion once."""
        if crop_bgr.ndim == 3 and crop_bgr.shape[2] == 4:
            crop_bgr = np.ascontiguousarray(crop_bgr[:, :, :3])   # live frames are BGRA, the art BGR
        table = [self.scores(crop_bgr, c) for c in centres]
        names: list[str | None] = [None] * len(centres)
        pairs = sorted(((s, i, n) for i, row in enumerate(table) for n, s in row.items()), reverse=True)
        used: set[str] = set()
        for s, i, n in pairs:
            if names[i] is not None or n in used or s < MIN_SCORE:
                continue
            others = sorted((v for k, v in table[i].items() if k != n), reverse=True)
            if others and s - others[0] < MIN_MARGIN:
                continue
            names[i] = n
            used.add(n)
        return names
