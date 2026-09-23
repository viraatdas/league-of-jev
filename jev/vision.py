"""Main-screen perception from health bars and HUD icons. Colour masks only, no model.

Every unit in League draws a flat health bar above it: enemies red, allies blue, the own
champion yellow. A bar is a coloured fill with a 1 px dark frame, so a coloured strip with
dark rows directly above and below it is a bar and not scenery. The fill width over the
bar width is the unit's HP. The unit itself stands a fixed offset below its bar.

Also reads the HUD: an ability icon that is lit is ready, a dark one is on cooldown,
unlearned, or (for Yasuo's R) has no airborne target.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from jev import config


@dataclass
class Unit:
    kind: str                 # minion | champion | monster (large jungle monster)
    team: str                 # enemy | ally | self
    x: float                  # screen px of the unit's body (click here)
    y: float
    hp: float                 # 0..1 from the bar fill
    bar: tuple[int, int, int, int]  # x, y, w, h of the fill

    def dist_px(self, x: float, y: float) -> float:
        return ((self.x - x) ** 2 + (self.y - y) ** 2) ** 0.5


@dataclass
class Hud:
    ready: dict[str, bool] = field(default_factory=dict)   # Q W E R D F -> lit
    lit: dict[str, float] = field(default_factory=dict)    # share of bright pixels per icon
    q_sig: tuple[float, float, float] = (0.0, 0.0, 0.0)    # Q icon mean H, S, V (tells Q3 apart)
    items_ready: dict[int, bool] = field(default_factory=dict)  # API slot 0-6 -> icon lit (active off cooldown)
    items_lit: dict[int, float] = field(default_factory=dict)


@dataclass
class View:
    units: list[Unit] = field(default_factory=list)
    me: Unit | None = None
    hud: Hud = field(default_factory=Hud)
    ms: float = 0.0
    ts: float = 0.0

    def enemies(self, kind: str) -> list[Unit]:
        return [u for u in self.units if u.team == "enemy" and u.kind == kind]

    def allies(self, kind: str) -> list[Unit]:
        return [u for u in self.units if u.team == "ally" and u.kind == kind]


def _masks(hsv: np.ndarray) -> dict[str, np.ndarray]:
    """Team colour masks (uint8 0/255). cv2.inRange runs vectorised in C, ~10x numpy comparisons."""
    red = cv2.inRange(hsv, (0, 120, 110), (8, 255, 255))
    red |= cv2.inRange(hsv, (172, 120, 110), (180, 255, 255))
    return {
        "enemy": red,
        "ally": cv2.inRange(hsv, (92, 110, 120), (128, 255, 255)),
        "self": cv2.inRange(hsv, (18, 120, 150), (34, 255, 255)),
    }


def to_bgr(img: np.ndarray) -> np.ndarray:
    """Capture frames are BGRA; saved screenshots are BGR."""
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR) if img.ndim == 3 and img.shape[2] == 4 else img


class VisionReader:
    def __init__(self, geo: config.Geometry = config.GEOMETRY, vc: config.Vision = config.VISION) -> None:
        self.geo, self.vc = geo, vc
        x0, y0, x1, y1 = vc.view
        self.view = (x0, y0, x1, y1)

    # -- health bars ------------------------------------------------------------------
    def _bars(self, masks: dict[str, np.ndarray], dark: np.ndarray, ox: int, oy: int) -> list[Unit]:
        """All teams in one pass per bar size: connected components on the union of the colour
        masks, each bar's team read from the colour inside its fill. Minion bars come from the raw
        mask (neighbouring bars stay apart); champion bars from a mask closed horizontally,
        because their 1000-HP tick marks split the fill."""
        union = masks["enemy"] | masks["ally"] | masks["self"]
        _, _, stats, _ = cv2.connectedComponentsWithStats(union, connectivity=4)
        st = stats[1:]
        out = self._bars_in(st, union.shape, masks, dark, ox, oy, "minion")
        out += self._bars_in(self._merge_ticks(st), union.shape, masks, dark, ox, oy, "champion")
        return out

    def _merge_ticks(self, st: np.ndarray) -> np.ndarray:
        """Champion bar fills are split by 1-2 px tick marks: join champion-height pieces that sit
        on the same rows with a gap of at most 3 px (same result as a horizontal closing, without
        a second connected-components pass)."""
        lo, hi = self.vc.champ_bar_h
        c = st[(st[:, 3] >= lo - 1) & (st[:, 3] <= hi + 1)]
        if len(c) == 0:
            return c
        c = c[np.lexsort((c[:, 0], c[:, 1]))]
        merged = [list(c[0])]
        for x, y, w, h, a in c[1:]:
            m = merged[-1]
            if abs(y - m[1]) <= 1 and abs(h - m[3]) <= 1 and 0 <= x - (m[0] + m[2]) <= 3:
                gap = x - (m[0] + m[2])
                m[2] = x + w - m[0]
                m[3] = max(m[3], h)
                m[4] += a + gap * h  # count the tick pixels as filled, as the closing did
            else:
                merged.append([x, y, w, h, a])
        return np.array(merged, dtype=np.int64)

    def _bars_in(self, st: np.ndarray, shape: tuple[int, int], masks: dict[str, np.ndarray], dark: np.ndarray, ox: int, oy: int, want: str) -> list[Unit]:
        vc = self.vc
        out: list[Unit] = []
        H, W = shape
        if len(st) == 0:
            return out
        if want == "minion":
            (lo_h, hi_h), lo_w, hi_w = vc.minion_bar_h, 2, vc.minion_bar_w + 2
            kind, full, (dx, dy) = "minion", vc.minion_bar_w, vc.minion_body_offset
        else:
            (lo_h, hi_h), lo_w, hi_w = vc.champ_bar_h, 3, vc.champ_bar_w + 4
            kind, full, (dx, dy) = "champion", vc.champ_bar_w, vc.champ_body_offset
        # Vectorised size filter: only plausible bars reach the per-bar checks. Bars are solid.
        keep = np.nonzero((st[:, 3] >= lo_h) & (st[:, 3] <= hi_h) & (st[:, 2] >= lo_w) & (st[:, 2] <= hi_w)
                          & (st[:, 4] >= 0.7 * st[:, 2] * st[:, 3]) & (st[:, 1] >= 1) & (st[:, 1] + st[:, 3] < H))[0]
        for i in keep:
            x, y, w, h, _area = (int(v) for v in st[i])
            unit_kind, unit_full = kind, full
            if kind == "champion":
                # A champion bar has its level box just left of it: a dark block. Without one, a
                # champion-height red bar is a large jungle monster (buffs, gromp, krugs, raptors...).
                box = dark[y:y + h, max(0, x - 20):max(0, x - 4)]
                if box.size == 0 or box.mean() < 0.35:
                    row = y + h // 2
                    if not masks["enemy"][row, x:x + w].any():
                        continue
                    run = 0
                    while x + w + run < W and dark[row, x + w + run] and run < 160:
                        run += 1
                    unit_kind, unit_full = "monster", max(w, w + run)
            # Frame check: the dark frame runs above and below the whole bar, not just the fill,
            # and closes on the left. Red damage numbers and scenery fail this.
            full = unit_full
            x_end = min(W, x + full + 1)
            above = dark[y - 1, max(0, x - 1):x_end].mean()
            below = dark[y + h, max(0, x - 1):x_end].mean()
            # (The left edge of the frame renders lighter on some settings, so only above/below are checked.)
            if above < 0.55 or below < 0.55:
                continue
            if w < full - 1:
                # The empty part of the bar is dark too.
                empty = dark[y + h // 2, x + w + 1:x_end - 1]
                if empty.size and empty.mean() < 0.5:
                    continue
            # Team from the fill's colour along its middle row.
            row = y + h // 2
            counts = {t: int(np.count_nonzero(m[row, x:x + w])) for t, m in masks.items()}
            team = max(counts, key=counts.get)
            if counts[team] == 0:
                continue
            hp = min(1.0, w / full)
            out.append(Unit(unit_kind, team, ox + x + full / 2 + dx, oy + y + h / 2 + dy, hp, (ox + x, oy + y, w, h)))
        return out

    def read_units(self, frame: np.ndarray) -> tuple[list[Unit], Unit | None]:
        x0, y0, x1, y1 = self.view
        hsv = cv2.cvtColor(to_bgr(frame[y0:y1, x0:x1]), cv2.COLOR_BGR2HSV)
        dark = hsv[:, :, 2] < self.vc.frame_dark_v
        # Blank the minimap corner and the HUD so their icons are never read as units.
        mx, my, _ = self.geo.minimap or (x1, y1, 0)
        hx0, hy0, hx1, hy1 = self.vc.hud_block
        masks = _masks(hsv)
        for m in masks.values():
            m[max(0, my - 20 - y0):, max(0, mx - 20 - x0):] = 0
            m[max(0, hy0 - y0):, max(0, hx0 - x0):max(0, hx1 - x0)] = 0
            cx0, cy0, cx1, cy1 = self.vc.chat_block
            m[max(0, cy0 - y0):max(0, cy1 - y0), max(0, cx0 - x0):max(0, cx1 - x0)] = 0
        found = self._bars(masks, dark, x0, y0)
        units = [u for u in found if u.team != "self"]
        mine = [u for u in found if u.team == "self" and u.kind == "champion"]
        me: Unit | None = None
        if mine:
            cx, cy = self.champion_px()
            me = min(mine, key=lambda u: u.dist_px(cx, cy))
        return units, me

    def champion_px(self) -> tuple[float, float]:
        return float(self.geo.champion_px[0]), float(self.geo.champion_px[1])

    # -- HUD --------------------------------------------------------------------------
    def read_hud(self, frame: np.ndarray) -> Hud:
        hud = Hud()
        r = self.vc.icon_half
        for name, (cx, cy) in zip("QWERDF", self.vc.hud_icons):
            patch = frame[cy - r:cy + r, cx - r:cx + r]
            if patch.size == 0:
                continue
            hsv = cv2.cvtColor(to_bgr(np.ascontiguousarray(patch)), cv2.COLOR_BGR2HSV)
            lit = float((hsv[:, :, 2] > 150).mean())
            hud.lit[name] = lit
            hud.ready[name] = lit >= self.vc.icon_ready_lit
            if name == "Q":
                hud.q_sig = (float(np.median(hsv[:, :, 0])), float(hsv[:, :, 1].mean()), float(hsv[:, :, 2].mean()))
        r = self.vc.item_half
        for slot, (cx, cy) in enumerate(self.vc.item_slots):
            patch = frame[cy - r:cy + r, cx - r:cx + r]
            if patch.size == 0:
                continue
            v = cv2.cvtColor(to_bgr(np.ascontiguousarray(patch)), cv2.COLOR_BGR2HSV)[:, :, 2]
            lit = float((v > 150).mean())
            hud.items_lit[slot] = lit
            hud.items_ready[slot] = lit >= self.vc.item_ready_lit
        return hud

    def read(self, frame: np.ndarray) -> View:
        t0 = time.perf_counter()
        units, me = self.read_units(frame)
        hud = self.read_hud(frame)
        return View(units=units, me=me, hud=hud, ms=(time.perf_counter() - t0) * 1000, ts=time.time())

    def annotate(self, frame: np.ndarray, view: View) -> np.ndarray:
        out = frame.copy()
        colors = {"enemy": (0, 0, 255), "ally": (255, 160, 0), "self": (0, 255, 255)}
        for u in view.units + ([view.me] if view.me else []):
            bx, by, bw, bh = u.bar
            cv2.rectangle(out, (bx, by), (bx + bw, by + bh), colors[u.team], 1)
            cv2.circle(out, (int(u.x), int(u.y)), 6 if u.kind == "minion" else 14, colors[u.team], 2)
            cv2.putText(out, f"{int(u.hp * 100)}", (int(u.x) + 8, int(u.y)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, colors[u.team], 1)
        y = 40
        for k, v in view.hud.ready.items():
            cv2.putText(out, f"{k}:{'ready' if v else '-'} {view.hud.lit.get(k, 0):.2f}", (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            y += 18
        return out


def px_to_units(dpx: float) -> float:
    return dpx / config.VISION.px_per_unit


def units_to_px(du: float) -> float:
    return du * config.VISION.px_per_unit
