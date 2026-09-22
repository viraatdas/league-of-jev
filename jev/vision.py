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
    kind: str                 # minion | champion
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
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return {
        "enemy": (((h <= 8) | (h >= 172)) & (s >= 120) & (v >= 110)),
        "ally": ((h >= 92) & (h <= 128) & (s >= 110) & (v >= 120)),
        "self": ((h >= 18) & (h <= 34) & (s >= 120) & (v >= 150)),
    }


class VisionReader:
    def __init__(self, geo: config.Geometry = config.GEOMETRY, vc: config.Vision = config.VISION) -> None:
        self.geo, self.vc = geo, vc
        x0, y0, x1, y1 = vc.view
        self.view = (x0, y0, x1, y1)

    # -- health bars ------------------------------------------------------------------
    def _bars(self, mask: np.ndarray, dark: np.ndarray, team: str, ox: int, oy: int) -> list[Unit]:
        # Minion bars are read from the raw mask (neighbouring bars stay apart); champion bars
        # from a mask closed horizontally, because their 1000-HP tick marks split the fill.
        raw = mask.astype(np.uint8)
        closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((1, 3), np.uint8))
        out = self._bars_in(raw, dark, team, ox, oy, "minion")
        out += self._bars_in(closed, dark, team, ox, oy, "champion")
        return out

    def _bars_in(self, mask: np.ndarray, dark: np.ndarray, team: str, ox: int, oy: int, want: str) -> list[Unit]:
        vc = self.vc
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        out: list[Unit] = []
        H, W = mask.shape
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if h < vc.minion_bar_h[0] or h > vc.champ_bar_h[1] or w < 2 or w > vc.champ_bar_w + 4:
                continue
            if area < 0.7 * w * h:  # bars are solid
                continue
            if y < 1 or y + h >= H:
                continue
            if want == "minion" and vc.minion_bar_h[0] <= h <= vc.minion_bar_h[1] and w <= vc.minion_bar_w + 2:
                kind, full = "minion", vc.minion_bar_w
                dx, dy = vc.minion_body_offset
            elif want == "champion" and vc.champ_bar_h[0] <= h <= vc.champ_bar_h[1] and w >= 3:
                kind, full = "champion", vc.champ_bar_w
                dx, dy = vc.champ_body_offset
                # A champion bar has its level box just left of it: a dark block.
                box = dark[y:y + h, max(0, x - 20):max(0, x - 4)]
                if box.size == 0 or box.mean() < 0.35:
                    continue
            else:
                continue
            # Frame check: the dark frame runs above and below the whole bar, not just the fill,
            # and closes on the left. Red damage numbers and scenery fail this.
            x_end = min(W, x + full + 1)
            above = dark[y - 1, max(0, x - 1):x_end].mean()
            below = dark[y + h, max(0, x - 1):x_end].mean()
            left = dark[y:y + h, max(0, x - 1)].mean()
            if above < 0.55 or below < 0.55 or left < 0.5:
                continue
            if w < full - 1:
                # The empty part of the bar is dark too.
                empty = dark[y + h // 2, x + w + 1:x_end - 1]
                if empty.size and empty.mean() < 0.5:
                    continue
            hp = min(1.0, w / full)
            cx = ox + x + full / 2 + dx
            cy = oy + y + h / 2 + dy
            out.append(Unit(kind, team if not (team == "self") else "self", cx, cy, hp, (ox + x, oy + y, w, h)))
        return out

    def read_units(self, frame: np.ndarray) -> tuple[list[Unit], Unit | None]:
        x0, y0, x1, y1 = self.view
        crop = frame[y0:y1, x0:x1]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        dark = hsv[:, :, 2] < self.vc.frame_dark_v
        # Blank the minimap corner so its icons are never read as units.
        mx, my, _ = self.geo.minimap or (x1, y1, 0)
        units: list[Unit] = []
        me: Unit | None = None
        for team, m in _masks(hsv).items():
            m = m.copy()
            m[max(0, my - 20 - y0):, max(0, mx - 20 - x0):] = False
            hx0, hy0, hx1, hy1 = self.vc.hud_block
            m[max(0, hy0 - y0):, max(0, hx0 - x0):max(0, hx1 - x0)] = False
            found = self._bars(m, dark, team, x0, y0)
            if team == "self":
                champs = [u for u in found if u.kind == "champion"]
                if champs:
                    cx, cy = self.champion_px()
                    me = min(champs, key=lambda u: u.dist_px(cx, cy))
                continue
            units.extend(found)
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
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            lit = float((hsv[:, :, 2] > 150).mean())
            hud.lit[name] = lit
            hud.ready[name] = lit >= self.vc.icon_ready_lit
            if name == "Q":
                hud.q_sig = (float(np.median(hsv[:, :, 0])), float(hsv[:, :, 1].mean()), float(hsv[:, :, 2].mean()))
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
