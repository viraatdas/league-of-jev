"""Tunables. Screen geometry is in physical pixels of the captured frame; screen.py converts."""
from __future__ import annotations

from dataclasses import dataclass

# Summoner's Rift, map units. Approximate; the pathfinder does the real work.
MAP_W, MAP_H = 14820.0, 14881.0
BLUE_FOUNTAIN = (400.0, 400.0)
RED_FOUNTAIN = (14300.0, 14400.0)
MID_CENTER = (7400.0, 7400.0)
BLUE_MID_T1 = (5846.0, 6396.0)
RED_MID_T1 = (8955.0, 8510.0)

# Progress along the mid-lane diagonal, 0 = own fountain, 1 = enemy fountain.
LANE_CENTER = 0.50
OWN_TOWER = 0.42
ENEMY_TOWER = 0.60
MAX_ADVANCE = 0.53          # never walk past this on your own
PUSH_ADVANCE = 0.57         # push_tower may go this far (minions tank the tower)
DIAGONAL_UNITS = 19660.0    # fountain to fountain


@dataclass
class Geometry:
    """Where things are on screen. Calibrate with `jev snapshot` once the game runs."""

    # Optional minimap square (x, y, side) in frame pixels. None = use screen-relative clicks only.
    minimap: tuple[int, int, int] | None = None
    # Optional shop search box (x, y) in frame pixels. None = shopping disabled.
    shop_search: tuple[int, int] | None = None
    move_click_px: int = 420        # how far ahead to click when walking
    attack_move_px: int = 260       # how far ahead to attack-move at the wave
    q_cast_px: int = 300            # blind Q distance up the lane
    retreat_click_px: int = 480


@dataclass
class Timing:
    tick_hz: float = 5.0
    brain_hz: float = 1.0
    move_reissue_s: float = 1.0
    q_period_s: float = 1.6
    recall_channel_s: float = 8.6
    respawn_shop_s: float = 2.5
    damage_window_s: float = 3.0
    heavy_damage_pct: float = 12.0  # HP% lost within the window that counts as heavy
    retreat_hold_s: float = 7.0     # keep retreating this long after a safety trigger
    no_recall_after_base_s: float = 25.0


GEOMETRY = Geometry()
TIMING = Timing()
