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
from jev.lanes import Lane
from jev.minimap import Wave

# Typing an item name into the shop search is only done once a screen read confirms the shop panel
# is open, and slowly: in a live game fast typing lost focus after one letter and the rest of the
# name went into the game as hotkeys.
TYPED_SEARCH = True

YASUO_SKILL_ORDER = ["Q", "E", "Q", "W", "Q", "R", "Q", "E", "Q", "E", "R", "E", "E", "W", "W", "R", "W", "W"]
ABILITY_INDEX = {"Q": 1, "W": 2, "E": 3, "R": 4}


class Navigator:
    """Progress along the lane polyline: set from the minimap when it reads, dead reckoned between."""

    def __init__(self, lane_units: float = config.DIAGONAL_UNITS) -> None:
        self.progress = 0.0
        self._last_t: float | None = None
        self.lane_units = lane_units

    def reset_to_base(self) -> None:
        self.progress = 0.0
        self._last_t = None

    def moved(self, direction: int, move_speed: float, now: float) -> None:
        """direction +1 forward, -1 back, 0 standing. Call every tick while a move order is active."""
        if self._last_t is not None and direction:
            dt = max(0.0, min(now - self._last_t, 1.0))
            self.progress += direction * move_speed * dt / self.lane_units
            self.progress = max(0.0, min(1.0, self.progress))
        self._last_t = now

    def advance_toward(self, target: float, move_speed: float, now: float) -> None:
        """A minimap order to `target` is being followed; integrate progress toward it."""
        if self._last_t is not None:
            dt = max(0.0, min(now - self._last_t, 1.0))
            step = move_speed * dt / self.lane_units
            if abs(target - self.progress) <= step:
                self.progress = target
            else:
                self.progress += step if target > self.progress else -step
        self._last_t = now

    @property
    def pct(self) -> int:
        return int(round(self.progress * 100))


class Mechanics:
    def __init__(self, ctl: Controller, screen: Screen, kb: Keybinds, side: str, geo: Geometry = config.GEOMETRY,
                 timing: Timing = config.TIMING, lane: Lane | None = None, skill_order: list[str] | None = None) -> None:
        self.ctl, self.screen, self.kb, self.geo, self.timing = ctl, screen, kb, geo, timing
        self.side = side  # ORDER (blue, bottom-left) or CHAOS (red, top-right)
        self.lane = lane or Lane("mid", side)
        self.skill_order = skill_order or YASUO_SKILL_ORDER
        self.nav = Navigator(self.lane.L)
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

    @property
    def fwd(self) -> tuple[float, float]:
        """Screen direction up the lane at the current position."""
        return self.lane.screen_dir(self.nav.progress)

    def _lane_point(self, progress: float) -> tuple[float, float]:
        """Map point at `progress` along the lane from the own fountain to the enemy one."""
        return self.lane.point(progress)

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

    aim_dir: tuple[float, float] | None = None  # screen-space unit vector toward the nearest enemy unit

    blind_q_enabled = True  # the loop turns it off when screen vision runs (then Q follows what is seen)

    def blind_q(self, now: float) -> None:
        # v0 behaviour from before vision: Q up the lane on a timer. With vision it only put Q on
        # cooldown before real last hits and bypassed Yasuo's Q-stack count.
        if not self.blind_q_enabled or now - self._last_q < self.timing.q_period_s:
            return
        self._last_q = now
        if self.aim_dir is not None:
            cx, cy = self._center()
            x = min(max(cx + self.aim_dir[0] * self.geo.q_cast_px, 8), self.screen.px_w - 8)
            y = min(max(cy + self.aim_dir[1] * self.geo.q_cast_px, 8), self.screen.px_h * 0.80)
            x, y = self.screen.to_points(x, y)
            self.last_action = "Q at target"
        else:
            x, y = self._ahead(self.geo.q_cast_px, 1)
            self.last_action = "Q up the lane"
        self._snapped(lambda: self._cast(1, x, y))

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

    def go_map(self, target: tuple[float, float], now: float, attack: bool = False, every: float = 1.0) -> bool:
        """Move (or attack-move) to any map point through the minimap; the game paths there."""
        pt = self._minimap_point(*target)
        if pt is None:
            return False
        if now - self._last_move < every:
            return True
        self._last_move = now
        if attack:
            self.ctl.move(*pt)
            self.ctl.press(self.kb.attack_move)
            self.ctl.click(*pt, "left")
        else:
            self.ctl.move_to(*pt)
        self.last_action = f"{'attack-move' if attack else 'travel'} to {int(target[0])},{int(target[1])}"
        return True

    def level_ability(self, ab: str) -> None:
        """Spend a skill point on `ab` (Q W E R): the level key when keys land, else the HUD chevron."""
        idx = ABILITY_INDEX[ab]
        if self.ctl.keys_ok():
            self.ctl.press(self.kb.level(idx))
        else:
            self.ctl.click(*self._pt(self.geo.level_chevrons[idx - 1]), "left")
        self.last_action = f"level {ab}"

    # -- behaviours ---------------------------------------------------------------------
    def go_lane(self, move_speed: float, now: float) -> None:
        if not self.go_progress(self.lane.own_tower, move_speed, now, attack=False):
            self.walk(1, move_speed, now)

    def farm(self, move_speed: float, now: float, aggression: float = 1.0, contact: bool = False, wave: Wave | None = None) -> None:
        """aggression is Jev's 0..2 score: passive stays nearer the own tower, aggressive holds
        closer to the enemy side of the wave. Without vision the wave is found by feel: minion
        chip damage means contact, so hold; no contact for a while means patrol along the lane."""
        ln, fr = self.lane, self.lane.frac
        if not hasattr(self, "_last_contact"):
            self._last_contact = now
            self._seek = 0.0
            self._seek_dir = 1
            self._seek_t = now
        # Patrol speed is per second (seek_step was tuned per tick at 5 Hz).
        dt, self._seek_t = min(1.0, now - self._seek_t), now
        if contact:
            self._last_contact = now
        elif now - self._last_contact > self.timing.seek_after_s:
            self._seek += self._seek_dir * self.timing.seek_step * 5.0 * dt
            if self._seek >= self.timing.seek_max:
                self._seek_dir = -1
            elif self._seek <= -self.timing.seek_back:
                self._seek_dir = 1
        if wave is not None and wave.enemy_front is not None:
            # Stand at the enemy minion front, a touch back; aggression leans in.
            limit = wave.enemy_front - fr(160) + (aggression - 1.0) * fr(200)
            self._seek = 0.0
        elif wave is not None and wave.ally_front is not None:
            limit = wave.ally_front - fr(200)
            self._seek = 0.0
        else:
            limit = ln.max_advance + (aggression - 1.0) * fr(690) + self._seek
        limit = max(ln.own_tower, min(limit, ln.hard_limit))
        if abs(self.nav.progress - limit) > fr(200):
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
        pa = self.lane.push_advance
        target = pa if self.nav.progress < pa - self.lane.frac(200) else self.nav.progress
        if not self.go_progress(target, move_speed, now, attack=True):
            self.walk(1, move_speed, now, attack=True)
        self.blind_q(now)

    def ensure_camera_locked(self, reader) -> str:
        """Snap the camera onto the champion (held key), read the minimap box, release, read again.
        If the box moves back the camera is unlocked: toggle the lock and verify. Returns a note."""
        from jev.minimap import dist

        def box():
            st = reader.read()
            return st.self_pos

        self.ctl.hold(self.kb.camera_snap, True)
        time.sleep(0.35)
        snapped = box()
        self.ctl.hold(self.kb.camera_snap, False)
        time.sleep(0.5)
        free = box()
        if snapped is None or free is None:
            return "camera: could not read minimap box"
        if dist(snapped, free) < 300:
            return "camera: locked"
        self.ctl.press(self.kb.camera_lock)
        time.sleep(0.6)
        after = box()
        if after is not None and dist(after, snapped) < 400:
            return "camera: was unlocked, now locked"
        self.ctl.press(self.kb.camera_lock)  # undo if the toggle made it worse
        return "camera: toggle did not lock (left as found)"

    def resync_to_own_tower(self) -> None:
        """Mid-game start: assume nothing about position, walk to the own tower and re-base the
        dead reckoning there."""
        self._seek = 0.0
        self.nav.progress = self.lane.own_tower

    def retreat(self, move_speed: float, now: float) -> None:
        target = self.lane.own_tower - self.lane.frac(400)
        if not self.go_progress(target, move_speed, now, attack=False):
            self.walk(-1, move_speed, now, px=self.geo.retreat_click_px)
        if abs(self.nav.progress - target) <= self.lane.frac(200):
            self.last_action = "holding at own tower"

    def step_back(self, move_speed: float, now: float) -> None:
        """Out of tower range: a short step back down the lane, not a full retreat."""
        target = max(self.lane.own_tower, self.nav.progress - self.lane.frac(1180))
        if not self.go_progress(target, move_speed, now, attack=False, force=True):
            self.walk(-1, move_speed, now)
        self.last_action = "step back from tower"

    def defend(self, move_speed: float, now: float) -> None:
        self.retreat(move_speed, now)
        self.blind_q(now)

    def group(self, move_speed: float, now: float) -> None:
        """v0: hold mid centre with the team; no cross-map travel yet."""
        if not self.go_progress(self.lane.center, move_speed, now, attack=True):
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
        """Channel time has passed; the loop also requires the minimap to put us in the fountain,
        because a recall that started late (walking first) lands later than the timer says."""
        return self.recall_started is not None and now - self.recall_started >= self.timing.recall_channel_s

    def cancel_recall(self) -> None:
        self.recall_started = None

    def level_up(self, ability_levels: dict[str, int]) -> str | None:
        """Click the HUD chevron (works in the background); the key is used only when it can land."""
        counts = {"Q": 0, "W": 0, "E": 0, "R": 0}
        for ab in self.skill_order:
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

    def shop_open(self) -> bool:
        """The shop is a large flat dark panel over the middle of the screen."""
        import cv2

        try:
            img = self.screen.grab()
            mid = cv2.cvtColor(img[150:850, 380:1350], cv2.COLOR_BGR2HSV)
            return float((mid[:, :, 2] < 70).mean()) > 0.55
        except Exception:  # noqa: BLE001
            return False

    def shop(self, item: str, items_now=None) -> bool:
        with self.ctl.slow():
            # Stand still first: an earlier move order walks the champion out of shop range while the
            # shop is open, and an out-of-range buy only queues the item.
            if self.ctl.keys_ok():
                self.ctl.press(self.kb.stop)
                time.sleep(0.15)
            return self._shop(item, items_now)

    def _shop(self, item: str, items_now=None) -> bool:
        """Buy `item`, verified through the API (items_now() returns the current item names).
        HUD shop button, then either a recommended card or the search box plus the first result
        tile; a double-click on a tile is what buys (verified live, Enter and PURCHASE do not).
        Closed with the X. The search box needs typing, so it is used only while keys land."""
        import collections

        before = collections.Counter(items_now()) if items_now else collections.Counter()

        def bought() -> bool:
            # The game API's inventory lags the shop by up to a couple of seconds: poll before
            # calling it a failure (a too-early check logged real purchases as failed and retried).
            # A different item than wanted is undone (a Lee Sin without Smite could not buy the
            # jungle pet and a fallback click bought a Doran's Blade, logged as the pet).
            if not items_now:
                return False
            for _ in range(10):
                time.sleep(0.25)
                new = collections.Counter(items_now()) - before
                if new:
                    if item.lower() in {n.lower() for n in new}:
                        return True
                    self.last_action = f"shop: wanted {item}, got {', '.join(new)}: undo"
                    if self.geo.shop_undo:
                        self.ctl.click(*self._pt(self.geo.shop_undo), "left")
                        time.sleep(0.8)
                    return False
            return False

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
        if low == "doran's blade" and self.geo.starter_card:
            card = self.geo.starter_card
        elif low == "boots" and self.geo.boots_card:
            card = self.geo.boots_card
        if card and self.geo.purchase_button:
            # Double-click on a tile buys it; the PURCHASE click is a harmless fallback.
            self.ctl.double_click(*self._pt(card))
            time.sleep(0.4)
            self.ctl.click(*self._pt(self.geo.purchase_button), "left")
            ok = bought()
        if not ok and TYPED_SEARCH and self.ctl.keys_ok() and self.geo.shop_search and self.shop_open():
            # Search, select the first result tile, then PURCHASE ITEM (Enter does not buy).
            self.ctl.click(*self._pt(self.geo.shop_search), "left")
            time.sleep(0.3)
            self.ctl.type_text(item, per_char_ms=90)
            time.sleep(1.3)  # let the results list update before clicking its first tile
            if self.geo.search_result:
                # Right-click buys an item in League's shop (a double-click queues it instead).
                self.ctl.click(*self._pt(self.geo.search_result), "right")
                ok = bought()
                if not ok and self.geo.purchase_button:
                    # Fallback: select the item, then the PURCHASE ITEM button.
                    self.ctl.click(*self._pt(self.geo.search_result), "left")
                    time.sleep(0.4)
                    self.ctl.click(*self._pt(self.geo.purchase_button), "left")
                    ok = bought()
        if self.geo.shop_close:
            self.ctl.click(*self._pt(self.geo.shop_close), "left")
        elif self.ctl.keys_ok():
            self.ctl.press(self.kb.shop)
        time.sleep(0.3)
        self.last_action = f"bought {item}" if ok else f"shop: could not buy {item}"
        return ok
