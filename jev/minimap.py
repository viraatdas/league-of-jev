"""Minimap reader: turns the minimap pixels back into map coordinates. Colour masks and
connected components only, a few milliseconds per read. This is the one place the bot looks
at the screen, and it looks only at the minimap square."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from jev import config
from jev.config import Geometry
from jev.screen import Screen


@dataclass
class MinimapState:
    self_pos: tuple[float, float] | None = None       # map units; from the camera box (locked camera)
    self_from_icon: tuple[float, float] | None = None  # map units; from the white-ringed icon if found
    enemy_champions: list[tuple[float, float]] = field(default_factory=list)
    ally_champions: list[tuple[float, float]] = field(default_factory=list)
    enemy_minions: list[tuple[float, float]] = field(default_factory=list)
    ally_minions: list[tuple[float, float]] = field(default_factory=list)
    ms: float = 0.0
    ts: float = 0.0

    @property
    def pos(self) -> tuple[float, float] | None:
        return self.self_pos or self.self_from_icon


class MinimapReader:
    def __init__(self, screen: Screen, geo: Geometry = config.GEOMETRY) -> None:
        self.screen = screen
        self.geo = geo
        x, y, side = geo.minimap
        self.x0, self.y0, self.side = int(x), int(y), int(side)
        self.region = {"left": self.x0, "top": self.y0, "width": self.side, "height": self.side}
        # Static-icon mask: structures and the two bases never count as units.
        self.static = np.full((self.side, self.side), 255, dtype=np.uint8)
        for mx, my in config.BLUE_STRUCTURES + config.RED_STRUCTURES:
            px, py = self.map_to_px(mx, my)
            cv2.circle(self.static, (int(px), int(py)), 11, 0, -1)
        base = int(self.side * 0.17)
        self.static[self.side - base:, :base] = 0          # blue base corner
        self.static[:base, self.side - base:] = 0          # red base corner
        # Persistence: pixels that stay red/blue for ~20 s are icons, not units.
        self._red_p = np.zeros((self.side, self.side), dtype=np.float32)
        self._blue_p = np.zeros((self.side, self.side), dtype=np.float32)
        self._last_t = 0.0

    # -- coordinates --------------------------------------------------------------------
    def px_to_map(self, px: float, py: float) -> tuple[float, float]:
        return px / self.side * config.MAP_W, (self.side - py) / self.side * config.MAP_H

    def map_to_px(self, mx: float, my: float) -> tuple[float, float]:
        return mx / config.MAP_W * self.side, self.side - my / config.MAP_H * self.side

    # -- capture --------------------------------------------------------------------------
    def grab(self) -> np.ndarray:
        shot = self.screen.sct.grab(self.region)
        return np.array(shot)[:, :, :3]

    # -- detection --------------------------------------------------------------------------
    @staticmethod
    def _components(mask: np.ndarray, min_area: int, max_area: int, max_box: int = 10_000):
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if min_area <= area <= max_area and w <= max_box and h <= max_box:
                out.append((float(cents[i][0]), float(cents[i][1]), area, w, h))
        return out

    def read(self, frame: np.ndarray | None = None) -> MinimapState:
        t0 = time.perf_counter()
        img = self.grab() if frame is None else frame
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        red = cv2.inRange(hsv, (0, 140, 140), (8, 255, 255)) | cv2.inRange(hsv, (172, 140, 140), (180, 255, 255))
        blue = cv2.inRange(hsv, (95, 120, 150), (125, 255, 255))
        white = cv2.inRange(hsv, (0, 0, 215), (180, 45, 255))
        red &= self.static
        blue &= self.static
        # Time-based decay (10 s time constant) so the filter is the same at any frame rate.
        now = time.time()
        a = 1.0 - math.exp(-min(1.0, now - self._last_t) / 10.0) if self._last_t else 0.02
        self._last_t = now
        self._red_p = (1 - a) * self._red_p + a * (red > 0)
        self._blue_p = (1 - a) * self._blue_p + a * (blue > 0)
        red[self._red_p > 0.85] = 0
        blue[self._blue_p > 0.85] = 0
        st = MinimapState(ts=time.time())

        def units(mask, minions, champs):
            # Minion dots: tiny solid blobs. Champion icons: ring-shaped blobs 16-30 px across.
            for cx, cy, area, w, h_ in self._components(mask, 3, 30, max_box=7):
                minions.append(self.px_to_map(cx, cy))
            for cx, cy, area, w, h_ in self._components(mask, 30, 900, max_box=34):
                if 14 <= w <= 34 and 14 <= h_ <= 34 and abs(w - h_) <= 8:
                    champs.append(self.px_to_map(cx, cy))

        units(red, st.enemy_minions, st.enemy_champions)
        units(blue, st.ally_minions, st.ally_champions)

        # Camera box: the largest white component shaped like a wide rectangle outline.
        best = None
        for cx, cy, area, w, h_ in self._components(white, 60, 4000):
            if 50 <= w <= 200 and 25 <= h_ <= 120 and w > h_:
                if best is None or area > best[2]:
                    best = (cx, cy, area, w, h_)
        if best is None:
            # The outline can be broken: clipped at the minimap edge, or cut by a champion icon on
            # one edge (which then merges with the icon into a taller blob). Pair any two box-wide
            # horizontal pieces that line up at about the box's height apart, using their outer
            # edges; if one end touches the minimap edge, use the box's usual width.
            n, _, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
            pieces = [tuple(int(v) for v in stats[i][:4]) for i in range(1, n)
                      if 55 <= stats[i][2] <= 140 and stats[i][3] <= 26]
            pair = None
            for a in pieces:
                for b in pieces:
                    gap = (b[1] + b[3]) - a[1]
                    if b[1] > a[1] and 30 <= gap <= 90 and abs(a[0] - b[0]) < 10 and abs(a[2] - b[2]) < 14:
                        if pair is None or a[2] + b[2] > pair[0][2] + pair[1][2]:
                            pair = (a, b)
            if pair is not None:
                a, b = pair
                left, right = min(a[0], b[0]), max(a[0] + a[2], b[0] + b[2])
                top, bottom = a[1], b[1] + b[3]
                cx = (left + right) / 2
                if right >= self.side - 3:
                    cx = left + 47
                elif left <= 3:
                    cx = right - 47
                best = (cx, (top + bottom) / 2, 0, right - left, bottom - top)
        if best is not None:
            st.self_pos = self.px_to_map(best[0], best[1])
            # The own icon sits inside the camera box; drop it from the ally champion list.
            sx, sy = st.self_pos
            st.ally_champions = [p for p in st.ally_champions if (p[0] - sx) ** 2 + (p[1] - sy) ** 2 > 700 ** 2]
        # Own icon: a white ring roughly 14-24 px across.
        for cx, cy, area, w, h_ in self._components(white, 25, 400):
            if 12 <= w <= 26 and 12 <= h_ <= 26 and abs(w - h_) <= 6:
                st.self_from_icon = self.px_to_map(cx, cy)
                break
        st.ms = (time.perf_counter() - t0) * 1000
        return st

    def annotate(self, frame: np.ndarray, st: MinimapState) -> np.ndarray:
        out = frame.copy()
        for pts, color, r in ((st.enemy_minions, (0, 0, 255), 2), (st.ally_minions, (255, 128, 0), 2),
                              (st.enemy_champions, (0, 0, 255), 8), (st.ally_champions, (255, 128, 0), 8)):
            for mx, my in pts:
                px, py = self.map_to_px(mx, my)
                cv2.circle(out, (int(px), int(py)), r, color, 1)
        if st.self_pos:
            px, py = self.map_to_px(*st.self_pos)
            cv2.drawMarker(out, (int(px), int(py)), (255, 255, 255), cv2.MARKER_CROSS, 14, 1)
        if st.self_from_icon:
            px, py = self.map_to_px(*st.self_from_icon)
            cv2.circle(out, (int(px), int(py)), 10, (0, 255, 255), 1)
        return out


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@dataclass
class Wave:
    enemy_front: float | None = None   # progress of the enemy minion nearest to us in our lane
    ally_front: float | None = None    # progress of our minion furthest up the lane
    enemy_count: int = 0
    ally_count: int = 0


def lane_wave(st: MinimapState, lane, band: float = 1500.0) -> Wave:
    """Minion fronts along `lane` (a lanes.Lane): minions within `band` units of the lane path."""
    w = Wave()
    en = [pr for pr, d in (lane.project(p) for p in st.enemy_minions) if d < band]
    al = [pr for pr, d in (lane.project(p) for p in st.ally_minions) if d < band]
    w.enemy_count, w.ally_count = len(en), len(al)
    if en:
        w.enemy_front = min(en)
    if al:
        w.ally_front = max(al)
    return w


def lane_progress(pos: tuple[float, float], side: str) -> tuple[float, float]:
    """Mid-lane progress (kept for callers that predate lanes.Lane)."""
    from jev.lanes import Lane

    return Lane("mid", side).project(pos)
