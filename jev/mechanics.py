"""Blind mechanics: turn an intent into clicks and keys without reading the screen.

With the camera locked the champion sits at the screen centre. Mid lane is the diagonal from
base to base, so 'forward' is a fixed screen direction per side. Position along the lane is
dead reckoned from move speed and time, and reset at every respawn or completed recall.
"""
from __future__ import annotations

import math
import time

from jev import config
from jev.config import Geometry, Timing
from jev.control import Controller
from jev.keybinds import Keybinds
from jev.screen import Screen

YASUO_SKILL_ORDER = ["Q", "E", "Q", "W", "Q", "R", "Q", "E", "Q", "E", "R", "E", "E", "W", "W", "R", "W", "W"]
ABILITY_INDEX = {"Q": 1, "W": 2, "E": 3, "R": 4}


class Navigator:
    """Dead-reckoned progress along the mid diagonal."""

    def __init__(self) -> None:
        self.progress = 0.0
        self._last_t: float | None = None

    def reset_to_base(self) -> None:
        self.progress = 0.0
        self._last_t = None

    def moved(self, direction: int, move_speed: float, now: float) -> None:
        """direction +1 forward, -1 back, 0 standing. Call every tick while a move order is active."""
        if self._last_t is not None and direction:
            dt = max(0.0, min(now - self._last_t, 1.0))
            self.progress += direction * move_speed * dt / config.DIAGONAL_UNITS
            self.progress = max(0.0, min(1.0, self.progress))
        self._last_t = now

    @property
    def pct(self) -> int:
        return int(round(self.progress * 100))


class Mechanics:
    def __init__(self, ctl: Controller, screen: Screen, kb: Keybinds, side: str, geo: Geometry = config.GEOMETRY, timing: Timing = config.TIMING) -> None:
        self.ctl, self.screen, self.kb, self.geo, self.timing = ctl, screen, kb, geo, timing
        self.side = side  # ORDER (blue, bottom-left) or CHAOS (red, top-right)
        f = 1 / math.sqrt(2)
        self.fwd = (f, -f) if side == "ORDER" else (-f, f)
        self.nav = Navigator()
        self._last_move = 0.0
        self._last_q = 0.0
        self._move_dir = 0
        self.recall_started: float | None = None
        self.last_action = ""

    # -- geometry helpers ---------------------------------------------------------
    def _center(self) -> tuple[float, float]:
        return self.screen.center_px()

    def _ahead(self, px: float, sign: int = 1) -> tuple[float, float]:
        cx, cy = self._center()
        x = cx + sign * self.fwd[0] * px
        y = cy + sign * self.fwd[1] * px
        x = min(max(x, 8), self.screen.px_w - 8)
        y = min(max(y, 8), self.screen.px_h * 0.80)  # stay above the HUD
        return self.screen.to_points(x, y)

    def _minimap_point(self, mx: float, my: float) -> tuple[float, float] | None:
        if not self.geo.minimap:
            return None
        x0, y0, side = self.geo.minimap
        px = x0 + mx / config.MAP_W * side
        py = y0 + side - my / config.MAP_H * side
        return self.screen.to_points(px, py)

    def _own(self, blue_pt, red_pt):
        return blue_pt if self.side == "ORDER" else red_pt

    # -- primitive orders -----------------------------------------------------------
    def walk(self, direction: int, move_speed: float, now: float, attack: bool = False, px: float | None = None) -> None:
        """Keep a move (or attack-move) order alive in the given lane direction."""
        self._move_dir = direction
        self.nav.moved(direction, move_speed, now)
        if now - self._last_move < self.timing.move_reissue_s:
            return
        self._last_move = now
        dist = px or (self.geo.attack_move_px if attack else self.geo.move_click_px)
        x, y = self._ahead(dist, direction)
        if attack:
            self.ctl.attack_move(self.kb.attack_move, x, y)
            self.last_action = f"attack-move {'fwd' if direction > 0 else 'back'}"
        else:
            self.ctl.move_to(x, y)
            self.last_action = f"move {'fwd' if direction > 0 else 'back'}"

    def hold(self, now: float) -> None:
        self._move_dir = 0
        self.nav.moved(0, 0.0, now)

    def blind_q(self, now: float) -> None:
        if now - self._last_q < self.timing.q_period_s:
            return
        self._last_q = now
        x, y = self._ahead(self.geo.q_cast_px, 1)
        self.ctl.cast(self.kb.ability(1), x, y, self.kb.quick_cast(1))
        self.last_action = "Q up the lane"

    def go_to_map(self, target, now: float) -> bool:
        """Right-click the minimap if calibrated. Returns False if no minimap geometry."""
        pt = self._minimap_point(*target)
        if pt is None:
            return False
        if now - self._last_move < self.timing.move_reissue_s * 2:
            return True
        self._last_move = now
        self.ctl.move_to(*pt)
        self.last_action = f"minimap move to {int(target[0])},{int(target[1])}"
        return True

    # -- behaviours ---------------------------------------------------------------------
    def go_lane(self, move_speed: float, now: float) -> None:
        if self.go_to_map(config.MID_CENTER, now):
            self.nav.moved(1, move_speed, now)
            return
        self.walk(1, move_speed, now)

    def farm(self, move_speed: float, now: float) -> None:
        if self.nav.progress < config.MAX_ADVANCE:
            self.walk(1, move_speed, now, attack=True)
        else:
            self.hold(now)
            if now - self._last_move >= self.timing.move_reissue_s:
                self._last_move = now
                x, y = self._ahead(self.geo.attack_move_px * 0.5, 1)
                self.ctl.attack_move(self.kb.attack_move, x, y)
                self.last_action = "attack-move hold"
        self.blind_q(now)

    def trade(self, move_speed: float, now: float) -> None:
        self.blind_q(now)
        self.walk(1, move_speed, now, attack=True, px=self.geo.attack_move_px)

    def push(self, move_speed: float, now: float) -> None:
        if self.nav.progress < config.PUSH_ADVANCE:
            self.walk(1, move_speed, now, attack=True)
        else:
            self.hold(now)
            x, y = self._ahead(self.geo.attack_move_px * 0.6, 1)
            if now - self._last_move >= self.timing.move_reissue_s:
                self._last_move = now
                self.ctl.attack_move(self.kb.attack_move, x, y)
                self.last_action = "attack-move at tower"
        self.blind_q(now)

    def retreat(self, move_speed: float, now: float) -> None:
        own_t1 = self._own(config.BLUE_MID_T1, config.RED_MID_T1)
        if self.nav.progress > config.OWN_TOWER - 0.03:
            if not self.go_to_map(own_t1, now):
                self.walk(-1, move_speed, now, px=self.geo.retreat_click_px)
            else:
                self.nav.moved(-1, move_speed, now)
        else:
            self.hold(now)
            self.last_action = "holding at own tower"

    def defend(self, move_speed: float, now: float) -> None:
        self.retreat(move_speed, now)
        self.blind_q(now)

    def group(self, move_speed: float, now: float) -> None:
        """v0: hold mid centre with the team; no cross-map travel yet."""
        if self.nav.progress < config.LANE_CENTER - 0.02:
            self.walk(1, move_speed, now)
        elif self.nav.progress > config.LANE_CENTER + 0.02:
            self.walk(-1, move_speed, now)
        else:
            self.hold(now)

    def start_recall(self, now: float) -> None:
        if self.recall_started is None:
            self.ctl.press(self.kb.stop)
            time.sleep(0.05)
            self.ctl.press(self.kb.recall)
            self.recall_started = now
            self.hold(now)
            self.last_action = "recalling"

    def recall_done(self, now: float) -> bool:
        return self.recall_started is not None and now - self.recall_started >= self.timing.recall_channel_s

    def cancel_recall(self) -> None:
        self.recall_started = None

    def level_up(self, ability_levels: dict[str, int]) -> str | None:
        counts = {"Q": 0, "W": 0, "E": 0, "R": 0}
        for ab in YASUO_SKILL_ORDER:
            counts[ab] += 1
            if ability_levels.get(ab, 0) < counts[ab]:
                self.ctl.press(self.kb.level(ABILITY_INDEX[ab]))
                self.last_action = f"level {ab}"
                return ab
        return None

    def shop(self, item: str) -> bool:
        if not self.geo.shop_search:
            return False
        self.ctl.press(self.kb.shop)
        time.sleep(0.6)
        sx, sy = self.screen.to_points(*self.geo.shop_search)
        self.ctl.click(sx, sy, "left")
        time.sleep(0.15)
        self.ctl.type_text(item)
        time.sleep(0.4)
        self.ctl.key("return")
        time.sleep(0.3)
        self.ctl.key("escape")
        self.last_action = f"bought {item}"
        return True
