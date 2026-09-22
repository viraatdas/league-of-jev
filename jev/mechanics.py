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

    def advance_toward(self, target: float, move_speed: float, now: float) -> None:
        """A minimap order to `target` is being followed; integrate progress toward it."""
        if self._last_t is not None:
            dt = max(0.0, min(now - self._last_t, 1.0))
            step = move_speed * dt / config.DIAGONAL_UNITS
            if abs(target - self.progress) <= step:
                self.progress = target
            else:
                self.progress += step if target > self.progress else -step
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
        if self.geo.champion_px:
            return float(self.geo.champion_px[0]), float(self.geo.champion_px[1])
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

    def _lane_point(self, progress: float) -> tuple[float, float]:
        """Map point at `progress` along the mid diagonal from the own fountain to the enemy one."""
        a = self._own(config.BLUE_FOUNTAIN, config.RED_FOUNTAIN)
        b = self._own(config.RED_FOUNTAIN, config.BLUE_FOUNTAIN)
        return a[0] + (b[0] - a[0]) * progress, a[1] + (b[1] - a[1]) * progress

    def go_progress(self, target: float, move_speed: float, now: float, attack: bool = False, force: bool = False) -> bool:
        """Order a move (or attack-move) to the lane point at `target` via the minimap. Returns
        False when no minimap geometry is calibrated (caller falls back to screen clicks)."""
        pt = self._minimap_point(*self._lane_point(target))
        if pt is None:
            return False
        self.nav.advance_toward(target, move_speed, now)
        self._move_dir = 0
        if not force and now - self._last_move < self.timing.move_reissue_s:
            return True
        self._last_move = now
        if attack:
            self.ctl.move(*pt)
            self.ctl.press(self.kb.attack_move)
            time.sleep(0.02)
            self.ctl.click(*pt, "left")
            self.last_action = f"attack-move to lane {int(target * 100)}%"
        else:
            self.ctl.move_to(*pt)
            self.last_action = f"move to lane {int(target * 100)}%"
        return True

    # -- primitive orders -----------------------------------------------------------
    def _snapped(self, fn) -> None:
        """Run a screen-relative action while the camera-snap key is held (only possible when keys
        reach the game); otherwise rely on the camera lock and run it directly."""
        if not self.ctl.keys_ok():
            fn()
            return
        self.ctl.hold(self.kb.camera_snap, True)
        time.sleep(0.05)
        try:
            fn()
        finally:
            time.sleep(0.03)
            self.ctl.hold(self.kb.camera_snap, False)

    def _pt(self, xy) -> tuple[float, float]:
        return self.screen.to_points(float(xy[0]), float(xy[1]))

    def _attack_move(self, x: float, y: float) -> None:
        if self.ctl.keys_ok():
            self.ctl.attack_move(self.kb.attack_move, x, y)
        else:
            self.ctl.attack_move_click(x, y)

    def _cast(self, idx: int, x: float, y: float) -> None:
        """Ability by key when possible, else click its HUD icon and then the target point."""
        if self.ctl.keys_ok():
            self.ctl.cast(self.kb.ability(idx), x, y, self.kb.quick_cast(idx))
            return
        icon = self.geo.ability_icons[idx - 1]
        self.ctl.click(*self._pt(icon), "left")
        time.sleep(0.05)
        self.ctl.click(x, y, "left")

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
            self._snapped(lambda: self._attack_move(x, y))
            self.last_action = f"attack-move {'fwd' if direction > 0 else 'back'}"
        else:
            self._snapped(lambda: self.ctl.move_to(x, y))
            self.last_action = f"move {'fwd' if direction > 0 else 'back'}"

    def hold(self, now: float) -> None:
        self._move_dir = 0
        self.nav.moved(0, 0.0, now)

    def blind_q(self, now: float) -> None:
        if now - self._last_q < self.timing.q_period_s:
            return
        self._last_q = now
        x, y = self._ahead(self.geo.q_cast_px, 1)
        self._snapped(lambda: self._cast(1, x, y))
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
        if not self.go_progress(config.OWN_TOWER, move_speed, now, attack=False):
            self.walk(1, move_speed, now)

    def farm(self, move_speed: float, now: float, aggression: float = 1.0, contact: bool = False) -> None:
        """aggression is Jev's 0..2 score: passive stays nearer the own tower, aggressive holds
        closer to the enemy side of the wave. Without vision the wave is found by feel: minion
        chip damage means contact, so hold; no contact for a while means patrol along the lane."""
        if not hasattr(self, "_last_contact"):
            self._last_contact = now
            self._seek = 0.0
            self._seek_dir = 1
        if contact:
            self._last_contact = now
        elif now - self._last_contact > self.timing.seek_after_s:
            self._seek += self._seek_dir * self.timing.seek_step
            if self._seek >= self.timing.seek_max:
                self._seek_dir = -1
            elif self._seek <= -self.timing.seek_back:
                self._seek_dir = 1
        limit = config.MAX_ADVANCE + (aggression - 1.0) * 0.035 + self._seek
        limit = max(config.OWN_TOWER, min(limit, config.HARD_LIMIT))
        if abs(self.nav.progress - limit) > 0.01:
            if not self.go_progress(limit, move_speed, now, attack=True):
                self.walk(1 if limit > self.nav.progress else -1, move_speed, now, attack=True)
        else:
            # Hold: attack-move onto the spot so he fights what is in range without moving off.
            if not self.go_progress(self.nav.progress, move_speed, now, attack=True):
                self.hold(now)
                if now - self._last_move >= self.timing.move_reissue_s:
                    self._last_move = now
                    x, y = self._ahead(20, 1)
                    self._snapped(lambda: self._attack_move(x, y))
            self.last_action = "hold at lane %d%%" % int(self.nav.progress * 100)
        self.blind_q(now)

    def trade(self, move_speed: float, now: float) -> None:
        self.blind_q(now)
        self.walk(1, move_speed, now, attack=True, px=self.geo.attack_move_px)

    def push(self, move_speed: float, now: float) -> None:
        target = config.PUSH_ADVANCE if self.nav.progress < config.PUSH_ADVANCE - 0.01 else self.nav.progress
        if not self.go_progress(target, move_speed, now, attack=True):
            self.walk(1, move_speed, now, attack=True)
        self.blind_q(now)

    def resync_to_own_tower(self) -> None:
        """Mid-game start: assume nothing about position, walk to the own tower and re-base the
        dead reckoning there."""
        self._seek = 0.0
        self.nav.progress = config.OWN_TOWER

    def retreat(self, move_speed: float, now: float) -> None:
        target = config.OWN_TOWER - 0.02
        if not self.go_progress(target, move_speed, now, attack=False):
            self.walk(-1, move_speed, now, px=self.geo.retreat_click_px)
        if abs(self.nav.progress - target) <= 0.01:
            self.last_action = "holding at own tower"

    def defend(self, move_speed: float, now: float) -> None:
        self.retreat(move_speed, now)
        self.blind_q(now)

    def group(self, move_speed: float, now: float) -> None:
        """v0: hold mid centre with the team; no cross-map travel yet."""
        if not self.go_progress(config.LANE_CENTER, move_speed, now, attack=True):
            self.hold(now)

    def start_recall(self, now: float) -> None:
        if self.recall_started is None:
            if self.ctl.keys_ok():
                self.ctl.press(self.kb.stop)
                time.sleep(0.05)
                self.ctl.press(self.kb.recall)
            elif self.geo.recall_button:
                self.ctl.click(*self._pt(self.geo.recall_button), "left")
            self.recall_started = now
            self.hold(now)
            self.last_action = "recalling"

    def recall_done(self, now: float) -> bool:
        return self.recall_started is not None and now - self.recall_started >= self.timing.recall_channel_s

    def cancel_recall(self) -> None:
        self.recall_started = None

    def level_up(self, ability_levels: dict[str, int]) -> str | None:
        """Click the HUD chevron (works in the background); the key is used only when it can land."""
        counts = {"Q": 0, "W": 0, "E": 0, "R": 0}
        for ab in YASUO_SKILL_ORDER:
            counts[ab] += 1
            if ability_levels.get(ab, 0) < counts[ab]:
                idx = ABILITY_INDEX[ab]
                if self.ctl.keys_ok():
                    self.ctl.press(self.kb.level(idx))
                else:
                    self.ctl.click(*self._pt(self.geo.level_chevrons[idx - 1]), "left")
                self.last_action = f"level {ab}"
                return ab
        return None

    def shop(self, item: str, items_now=None) -> bool:
        """Buy `item`, verified through the API (items_now() returns the current item names).
        HUD shop button, then either a recommended card or the search box plus the first result
        tile; a double-click on a tile is what buys (verified live, Enter and PURCHASE do not).
        Closed with the X. The search box needs typing, so it is used only while keys land."""
        before = set(items_now()) if items_now else set()

        def bought() -> bool:
            if not items_now:
                return False
            time.sleep(0.5)
            return bool(set(items_now()) - before)

        if self.geo.shop_button:
            self.ctl.click(*self._pt(self.geo.shop_button), "left")
        elif self.ctl.keys_ok():
            self.ctl.press(self.kb.shop)
        else:
            return False
        time.sleep(0.9)
        ok = False
        low = item.lower()
        card = None
        if low.startswith("doran") and self.geo.starter_card:
            card = self.geo.starter_card
        elif ("boots" in low or "greaves" in low) and self.geo.boots_card:
            card = self.geo.boots_card
        if card and self.geo.purchase_button:
            # Double-click on a tile buys it; the PURCHASE click is a harmless fallback.
            self.ctl.double_click(*self._pt(card))
            time.sleep(0.4)
            self.ctl.click(*self._pt(self.geo.purchase_button), "left")
            ok = bought()
        if not ok and self.ctl.keys_ok() and self.geo.shop_search:
            # Search, select the first result tile, then PURCHASE ITEM (Enter does not buy).
            self.ctl.click(*self._pt(self.geo.shop_search), "left")
            time.sleep(0.3)
            self.ctl.type_text(item)
            time.sleep(0.8)
            if self.geo.search_result:
                self.ctl.double_click(*self._pt(self.geo.search_result))
            ok = bought()
        if self.geo.shop_close:
            self.ctl.click(*self._pt(self.geo.shop_close), "left")
        elif self.ctl.keys_ok():
            self.ctl.press(self.kb.shop)
        time.sleep(0.3)
        self.last_action = f"bought {item}" if ok else f"shop: could not buy {item}"
        return ok
