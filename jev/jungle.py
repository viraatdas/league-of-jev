"""Jungle: camp positions, respawn timers and the clear route for each side.

Camps are in map units for the blue side; red side mirrors them through the map centre.
Timers from our own clears: a camp we cleared is back after its respawn time, and every camp
first spawns at 1:30 game time. The route is the standard full clear starting at red buff;
after the first clear the next camp is the nearest one that is up, unless strategy sends the
jungler elsewhere (ganks and objectives use the strategy head's destination).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from jev import config

BLUE_CAMPS: dict[str, tuple[float, float]] = {
    "red": (7862.0, 4111.0), "krugs": (8394.0, 2641.0), "raptors": (6937.0, 5465.0),
    "wolves": (3780.0, 6443.0), "blue": (3872.0, 7900.0), "gromp": (2288.0, 8448.0),
}
BIG = {"red", "blue", "gromp", "krugs"}          # Smite-worthy camps (one large monster)
RESPAWN = {"red": 300.0, "blue": 300.0, "gromp": 135.0, "krugs": 135.0, "raptors": 135.0, "wolves": 135.0}
FIRST_SPAWN = 90.0
ROUTE = ["red", "krugs", "raptors", "wolves", "blue", "gromp"]


def camps(side: str) -> dict[str, tuple[float, float]]:
    if side == "ORDER":
        return dict(BLUE_CAMPS)
    return {k: (config.MAP_W - x, config.MAP_H - y) for k, (x, y) in BLUE_CAMPS.items()}


@dataclass
class JungleState:
    side: str
    cleared: dict[str, float] = field(default_factory=dict)   # camp -> game time cleared
    route_i: int = 0
    current: str | None = None
    arrived_at: float | None = None                           # game time we reached the current camp
    last_seen_monster: float = 0.0

    def up(self, camp: str, gt: float) -> bool:
        if gt < FIRST_SPAWN - 5:
            return False
        t = self.cleared.get(camp)
        return t is None or gt - t >= RESPAWN[camp]

    def next_camp(self, pos: tuple[float, float] | None, gt: float) -> str:
        """Follow the route on the first clear; afterwards the nearest camp that is up (or will be
        soonest)."""
        cs = camps(self.side)
        if self.route_i < len(ROUTE):
            return ROUTE[self.route_i]
        up = [c for c in cs if self.up(c, gt)]
        if up and pos is not None:
            return min(up, key=lambda c: math.dist(pos, cs[c]))
        if up:
            return up[0]
        return min(cs, key=lambda c: RESPAWN[c] - (gt - self.cleared.get(c, -1e9)))

    def mark_cleared(self, camp: str, gt: float) -> None:
        self.cleared[camp] = gt
        if self.route_i < len(ROUTE) and ROUTE[self.route_i] == camp:
            self.route_i += 1
        self.current, self.arrived_at = None, None
