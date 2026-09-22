"""Lanes as polylines from the own fountain to the enemy fountain, in map units.

Progress is the arc-length fraction along the lane (0 = own fountain, 1 = enemy fountain).
Every positional limit (own tower, lane centre, how far to push, where tower range starts)
is derived from the real tower positions in game units, so the same logic runs top, mid or
bot. Mid is the straight base-to-base diagonal, which reproduces the values tuned earlier.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from jev import config

F_BLUE, F_RED = config.BLUE_FOUNTAIN, config.RED_FOUNTAIN
BT, RT = config.BLUE_TOWERS, config.RED_TOWERS
# Tower list indexes: top 0-2, mid 3-5, bot 6-8 (outer, inner, inhibitor).
LANE_TOWER_BASE = {"top": 0, "mid": 3, "bot": 6}
LANE_LETTER = {"top": "L", "mid": "C", "bot": "R"}  # TurretKilled names: Turret_T2_C_05_A etc.

# Blue-side paths; red side walks the same path reversed.
PATHS = {
    "mid": [F_BLUE, F_RED],
    "bot": [F_BLUE, BT[8], BT[7], BT[6], (12900.0, 1800.0), RT[6], RT[7], RT[8], F_RED],
    "top": [F_BLUE, BT[2], BT[1], BT[0], (1800.0, 12900.0), RT[0], RT[1], RT[2], F_RED],
}

ROLE_LANE = {"MIDDLE": "mid", "UTILITY": "bot", "BOTTOM": "bot", "TOP": "top", "JUNGLE": "mid"}


@dataclass
class Lane:
    name: str
    side: str
    pts: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        path = list(PATHS[self.name])
        self.pts = path if self.side == "ORDER" else list(reversed(path))
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.L = self.cum[-1]
        own = BT if self.side == "ORDER" else RT
        enemy = RT if self.side == "ORDER" else BT
        base = LANE_TOWER_BASE[self.name]
        self.own_outer, self.enemy_outer = own[base], enemy[base]
        own_t = self.project(self.own_outer)[0]
        self.enemy_tower = self.project(self.enemy_outer)[0]
        self.own_tower = own_t + self.frac(200)
        self.center = (own_t + self.enemy_tower) / 2
        self.max_advance = self.center - self.frac(450)
        self.hard_limit = self.enemy_tower - self.frac(1350)
        self.push_advance = self.enemy_tower - self.frac(1150)

    # -- geometry ------------------------------------------------------------------------
    def frac(self, units: float) -> float:
        return units / self.L

    def units(self, frac: float) -> float:
        return frac * self.L

    def project(self, pos: tuple[float, float]) -> tuple[float, float]:
        """(progress 0..1, distance from the lane in map units)."""
        best = (0.0, float("inf"))
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg2 = dx * dx + dy * dy or 1.0
            t = max(0.0, min(1.0, ((pos[0] - a[0]) * dx + (pos[1] - a[1]) * dy) / seg2))
            px, py = a[0] + t * dx, a[1] + t * dy
            d = math.hypot(pos[0] - px, pos[1] - py)
            if d < best[1]:
                best = ((self.cum[i] + t * math.sqrt(seg2)) / self.L, d)
        return best

    def point(self, progress: float) -> tuple[float, float]:
        s = max(0.0, min(1.0, progress)) * self.L
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            seg = self.cum[i + 1] - self.cum[i]
            if s <= self.cum[i + 1] or i == len(self.pts) - 2:
                t = (s - self.cum[i]) / seg if seg else 0.0
                return a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        return self.pts[-1]

    def screen_dir(self, progress: float) -> tuple[float, float]:
        """Unit vector up the lane in screen coordinates (screen y points down)."""
        a, b = self.point(progress - self.frac(300)), self.point(progress + self.frac(300))
        dx, dy = b[0] - a[0], -(b[1] - a[1])
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n

    # -- labels for Jev -----------------------------------------------------------------
    def where_label(self, progress: float) -> str:
        if progress >= self.enemy_tower - self.frac(750):
            return f"pushed up near the enemy {self.name} tower"
        if progress >= self.center - self.frac(700):
            return f"at the middle of {self.name} lane"
        if progress >= self.own_tower - self.frac(800):
            return f"near my own {self.name} tower"
        return f"walking from base to {self.name} lane"

    def wave_label(self, front: float) -> str:
        if front < self.own_tower + self.frac(400):
            return "under my tower"
        if front < self.center - self.frac(300):
            return "on my side of the lane"
        if front < self.center + self.frac(300):
            return "at the middle"
        if front < self.enemy_tower - self.frac(750):
            return "on their side"
        return "under their tower"

    def enemy_tower_ids(self) -> dict[int, int]:
        """Index in the enemy tower list -> tower number in TurretKilled event names."""
        b = LANE_TOWER_BASE[self.name]
        return {b: 5, b + 1: 4, b + 2: 3}


def lane_for(role: str, default: str = "mid") -> str:
    return ROLE_LANE.get((role or "").upper(), default)
