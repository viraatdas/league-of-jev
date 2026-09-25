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
        # The camera box only: the white-ring icon read agreed with the box within 1000 units on 11
        # of 416 frames (g20-g29), and as a fallback it put Yasuo in bot lane mid-game (g29 top).
        return self.self_pos


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
            n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
            px_pts: list[tuple[float, float, float]] = []
            for i in range(1, n):
                area = int(stats[i, cv2.CC_STAT_AREA])
                w, h_ = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
                if area < 30:
                    continue
                if 20 <= w <= 34 and 20 <= h_ <= 34 and abs(w - h_) <= 8:  # a ring is ~31 px; wards and pings are smaller
                    px_pts.append((float(cents[i][0]), float(cents[i][1]), area))
                elif 34 < max(w, h_) <= 90 and min(w, h_) >= 26 and area >= 170 and 0.06 <= area / (w * h_) <= 0.35:
                    # Overlapping icons merge into one blob (a ring alone is ~32 px, ~140 px of area; the
                    # merged blob stays fat and hollow, unlike a line of minion dots):
                    # a bot lane duo read as nobody, and ganks there ended at once as "0 of us" (g21).
                    k = min(4, max(2, round(area / 125)))
                    ys, xs = np.nonzero(labels == i)
                    pts = np.float32(np.column_stack([xs, ys]))
                    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
                    _, _, centers = cv2.kmeans(pts, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
                    for c in centers:
                        px_pts.append((float(c[0]), float(c[1]), area / k))
            # One icon can yield several pieces (ring fragments, blue portrait art): keep one point per
            # 15 px (an icon's radius), largest first, and at most five per team.
            kept: list[tuple[float, float, float]] = []
            for x, y, a in sorted(px_pts, key=lambda t: -t[2]):
                if all((x - kx) ** 2 + (y - ky) ** 2 > 15 ** 2 for kx, ky, _ in kept):
                    kept.append((x, y, a))
            for x, y, _ in kept[:5]:
                champs.append(self.px_to_map(x, y))

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
            # A champion icon on the box's edge cuts that edge short: in the top-left corner (top lane)
            # the top edge started at x=31 while the bottom one started at 3, and the box was lost on
            # 19% of frames (g29). Edges pair when their left ends or their right ends line up; one of
            # them must be nearly the box's full width.
            pieces = [tuple(int(v) for v in stats[i][:4]) for i in range(1, n)
                      if 40 <= stats[i][2] <= 140 and stats[i][3] <= 26]
            pair = None
            for a in pieces:
                for b in pieces:
                    gap = (b[1] + b[3]) - a[1]
                    if b[1] <= a[1] or not 30 <= gap <= 90:
                        continue
                    twins = min(a[2], b[2]) >= 55 and abs(a[0] - b[0]) < 10 and abs(a[2] - b[2]) < 14
                    cut = max(a[2], b[2]) >= 70 and (abs(a[0] - b[0]) < 10
                                                     or abs((a[0] + a[2]) - (b[0] + b[2])) < 10)
                    if twins or cut:
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
            # The own icon sits inside the camera box; drop it from the ally champion list. Its red
            # parts (Lee Sin's headband) also read as an enemy icon 100-500 units from us, which kept
            # the fight head retreating from nobody in the jungle (g12): an enemy that close is on
            # screen anyway, so enemy blobs within 600 units go too.
            sx, sy = st.self_pos
            st.ally_champions = [p for p in st.ally_champions if (p[0] - sx) ** 2 + (p[1] - sy) ** 2 > 700 ** 2]
            st.enemy_champions = [p for p in st.enemy_champions if (p[0] - sx) ** 2 + (p[1] - sy) ** 2 > 600 ** 2]
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


class TowerWatch:
    """Lane towers standing or not, from their minimap icons: the TurretKilled events never moved our
    tower line in g22-g26 (no name matched), and Yasuo "retreated" to our dead outer mid tower and died
    there over and over (the same spot, 5896,6260, in four games). A tower is dead once it was seen
    standing (15+ icon pixels) and then shows none (<= 2) on five checks in a row (~10 s), with no
    champion icon on it; only frames with the camera box found count (the end screens have no minimap)."""

    ALIVE, GONE, CONFIRM = 15, 2, 5

    def __init__(self) -> None:
        self.seen = {}      # (team, i) -> seen standing
        self.gone = {}      # (team, i) -> consecutive empty checks
        self.dead: set[tuple[str, int]] = set()

    def update(self, crop_bgr: np.ndarray, reader: "MinimapReader", blue_icons: list | None = None,
               red_icons: list | None = None, ours: str | None = None) -> list[tuple[str, int]]:
        """`blue_icons` / `red_icons`: blue-team and red-team champion icons this frame (map units). An
        icon of the other colour on a tower hides its colour and would fake its death (an ally sieging
        their tower; calling it dead walks us into its range), so that tower is not read this time.
        An icon of its own colour can only make it look standing, never dead."""
        hsv = cv2.cvtColor(crop_bgr[:, :, :3], cv2.COLOR_BGR2HSV)
        new = []
        for team, towers in (("blue", config.BLUE_TOWERS), ("red", config.RED_TOWERS)):
            # Our towers: a false "dead" only moves our retreat line back (the safe way), so they are
            # called sooner and without the icon guard (Yasuo standing on the ruins kept it "standing").
            mine = ours is not None and team == ours
            cover = [] if mine else ((red_icons if team == "blue" else blue_icons) or [])
            confirm = 3 if mine else self.CONFIRM
            for i, t in enumerate(towers[:11]):     # the nine lane towers and the two nexus towers
                key = (team, i)
                if key in self.dead:
                    continue
                if any(dist(t, c) < 900 for c in cover):
                    continue  # covered: no answer this time
                px, py = reader.map_to_px(*t)
                win = hsv[max(0, int(py) - 9):int(py) + 10, max(0, int(px) - 9):int(px) + 10]
                h, s, v = win[..., 0], win[..., 1], win[..., 2]
                if team == "blue":
                    n = int(((h >= 88) & (h <= 112) & (s > 90) & (v > 110)).sum())
                else:
                    n = int((((h <= 8) | (h >= 170)) & (s > 120) & (v > 110)).sum())
                if n >= self.ALIVE:
                    self.seen[key], self.gone[key] = True, 0
                elif n <= self.GONE and self.seen.get(key):
                    self.gone[key] = self.gone.get(key, 0) + 1
                    if self.gone[key] >= confirm:
                        self.dead.add(key)
                        new.append(key)
                else:
                    self.gone[key] = 0
        return new


class PosFilter:
    """Drops a camera-box read that jumps farther than a champion moves (a dash or Flash is within
    1500 units), unless the new spot repeats for `confirm` reads in a row (a recall, a respawn). A
    box edge paired with the wrong line jumped the read 2000-3000 units for a frame (g26, g28)."""

    def __init__(self, confirm: int = 5, stale_s: float = 2.0) -> None:
        self.confirm, self.stale_s = confirm, stale_s
        self.good: tuple[float, tuple[float, float]] | None = None   # (ts, pos) last accepted
        self.cand: tuple[tuple[float, float], int] | None = None      # (pos, reads in a row)
        self.dropped = 0

    def apply(self, st: MinimapState) -> MinimapState:
        p = st.self_pos
        if p is None:
            return st
        g = self.good
        if g is None or st.ts - g[0] > self.stale_s or dist(p, g[1]) <= 1500 + 700 * (st.ts - g[0]):
            self.good, self.cand = (st.ts, p), None
            return st
        n = self.cand[1] + 1 if self.cand is not None and dist(self.cand[0], p) < 800 else 1
        if n >= self.confirm:
            self.good, self.cand = (st.ts, p), None
            return st
        self.cand = (p, n)
        self.dropped += 1
        st.self_pos = None
        return st


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
