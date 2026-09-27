"""The main loop: poll Riot, track HP, ask Jev once a second, act several times a second."""
from __future__ import annotations

import collections
import faulthandler
import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from jev import config, keybinds
from jev.brain import Brain, Decision
from jev.control import Controller, activate_game
from jev.mechanics import Mechanics
from jev.decisions import DecisionLog
from jev.brain import credit_wait, legal_level_ups
from jev.items import BuildPlan, ShopBrain, enemy_team
from jev.jungle import BIG, FIRST_SPAWN, JungleState, camps
from jev import places as map_places
from jev.kits import FIGHT_MODES, Kit, kit_for
from jev.lanes import LANE_LETTER, Lane, lane_for
from jev.micro import HP_MODEL, Micro, UnitTracker, build_scene
from jev.minimap import MinimapReader, MinimapState, PosFilter, TowerWatch, dist, lane_progress, lane_wave
from jev.riot_api import FixtureRecorder, RiotLiveClient
from jev.screen import Screen
from jev.state import Perception, build_state, find_me
from jev import actions
from jev.tactics import TacticalBrain, TacticInput, menu
from jev.fight import FightBrain, FightRead
from jev.fog import Whereabouts, jungler_of
from jev.iconid import IconMatcher
from jev.lanelearn import LaneLearner
from jev.enemies import EnemyKnowledge
from jev import macro
from jev import namereader
from jev.vision import View, VisionReader

console = Console()


def tower_from_event(name: str) -> tuple[str, int] | None:
    """A TurretKilled name to (team colour, tower index). This patch's names look like
    Turret_TChaos_L1_P3_2254202041_0: T{Order,Chaos}, lane L0 bot / L1 mid / L2 top, P3 outer /
    P2 inner / P1 inhibitor (matched against the minimap watch in g30); the older
    Turret_T2_C_05_A form is read too. None when the name does not parse."""
    import re as _re

    m = _re.match(r"Turret_T(Order|Chaos)_L(\d)_P(\d)", name)
    if m:
        team = "blue" if m.group(1) == "Order" else "red"
        if int(m.group(3)) >= 4:
            # the nexus towers (Turret_TOrder_L1_P4_..., g30); the second one is the other index
            return team, 9 if int(m.group(3)) == 4 else 10
        lane = {0: 2, 1: 1, 2: 0}.get(int(m.group(2)))
        tier = {3: 0, 2: 1, 1: 2}.get(int(m.group(3)))
        if lane is None or tier is None:
            return None
        return team, lane * 3 + tier
    m = _re.match(r"Turret_T([12])_([LCR])_(\d\d)_A", name)
    if m:
        team = "blue" if m.group(1) == "1" else "red"
        lane = {"L": 0, "C": 1, "R": 2}[m.group(2)]
        tier = {5: 0, 4: 1, 3: 2}.get(int(m.group(3))) if lane == 1 else {3: 0, 2: 1, 1: 2}.get(int(m.group(3)))
        return (team, lane * 3 + tier) if tier is not None else None
    return None


def inp_hp(data: dict) -> float:
    cs = (data.get("activePlayer") or {}).get("championStats") or {}
    return 100.0 * float(cs.get("currentHealth", 0.0)) / max(1.0, float(cs.get("maxHealth", 1.0)))


def choose_intent(d: Decision | None, state: dict, p: Perception, now: float, guard: "Guards") -> str:
    """Jev decides. Code adds only what a decision model cannot: a survival floor when HP is
    collapsing between Jev ticks, and mechanics constraints (no recall mid-lane, handled later)."""
    me = state["me"]
    if not me["alive"]:
        return "dead"
    if p.recalling:
        return "recall"
    since_dmg = p.seconds_since_damage if p.seconds_since_damage is not None else 99.0
    if me["hp_percent"] < 15 and since_dmg < 4:
        guard.retreat_until = now + 4.0  # survival floor, shorter than Jev's own retreat calls
    if p.hp_lost_recent_pct >= config.TIMING.heavy_damage_pct and now >= guard.fight_back_until:
        guard.retreat_until = now + 5.0  # tower-sized chunks: step out before the next shot
    # Under their tower is fine while our wave is there taking its shots, nobody of theirs is close and
    # we are healthy: the last hits there are safe. Stepping back every time left Yasuo at 50 CS at
    # 44:00 while the team kept their wave under the tower (g26).
    tower_farm_ok = (p.ally_minions_at_enemy_tower >= 3 and me["hp_percent"] >= 50
                     and (p.nearest_enemy_champion_units is None or p.nearest_enemy_champion_units >= 1000))
    if p.near_enemy_tower and (d is None or d.intent != "push_tower") and now >= guard.push_ok_until and not tower_farm_ok:
        guard.step_back_until = now + 2.5
    if p.nearest_enemy_champion_units is not None and p.nearest_enemy_champion_units < 600 and me["hp_percent"] < 30:
        guard.retreat_until = now + 5.0
    if now < guard.retreat_until:
        return "retreat"
    if now < guard.step_back_until:
        return "step_back"
    if d is None or now - d.ts > 6.0:
        # No fresh strategy from Jev (API down, out of credits): recall by rule, otherwise farm.
        gold = float(me.get("gold") or 0)
        enemy_near = p.nearest_enemy_champion_units is not None and p.nearest_enemy_champion_units < 1500
        # Items are the kills: at 16:36 in g19 Yasuo carried 2,082 unspent gold into fights.
        if (me["hp_percent"] < 35 and since_dmg > 4 and not enemy_near) or (gold >= 1100 and me["hp_percent"] < 65 and not enemy_near) \
                or (gold >= 1600 and not enemy_near):
            return "recall"
        return "farm"
    if d.danger >= 2.5:
        return "retreat"
    gold = float(me.get("gold") or 0)
    recall_bar = 0.65 if gold >= 1200 else 0.8  # a pile of unspent gold is worth a trip home sooner
    if d.intent == "recall" or (d.should_recall >= recall_bar and since_dmg > 6):
        return "recall"
    if d.intent in ("trade", "all_in"):
        return "trade"
    return d.intent


class _EventLog(collections.deque):
    """The last few event lines for the terminal panel, also appended to a plain file."""

    def __init__(self, maxlen: int, path: str | None) -> None:
        super().__init__(maxlen=maxlen)
        self.path = path

    def append(self, x) -> None:  # noqa: D401
        super().append(x)
        if self.path and not str(x).startswith(("move(", "click_", "key(", "press(", "hold(", "shift_click", "type(")):
            try:
                with open(self.path, "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {x}\n")
            except OSError:
                pass


class Guards:
    """Timers that keep code-level safety rules sticky across Jev ticks."""

    def __init__(self) -> None:
        self.retreat_until = 0.0
        self.step_back_until = 0.0
        self.left_base_at = 0.0
        self.last_level_t = 0.0
        self.resync_until = 0.0
        self.push_ok_until = 0.0
        self.fight_back_until = 0.0   # fighting a diver back: the heavy-damage and bleeding retreats wait


class HpTracker:
    def __init__(self, window_s: float) -> None:
        self.window = window_s
        self.samples: collections.deque[tuple[float, float]] = collections.deque()
        self.last_damage_t: float | None = None
        self._last_pct: float | None = None

    def update(self, hp_pct: float, now: float) -> float:
        if self._last_pct is not None and hp_pct < self._last_pct - 0.5:
            self.last_damage_t = now
        self._last_pct = hp_pct
        self.samples.append((now, hp_pct))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        return max(0.0, max(s[1] for s in self.samples) - hp_pct) if self.samples else 0.0

    def since_damage(self, now: float) -> float | None:
        return None if self.last_damage_t is None else now - self.last_damage_t


class Player:
    def __init__(self, dry_run: bool = False, quick_cast: bool | None = None, role: str = "", logfile=None, keep_front: bool = True,
                 forever: bool = False, tactic_hz: float = 8.0, champion: str | None = None, explore: float = 0.0,
                 decision_log: str | None = "logs/decisions.jsonl", save_frames_s: float = 0.0,
                 capture: str = "sck", capture_fps: int = 60, frames_dir: str = "snapshots/live") -> None:
        self.dry_run = dry_run
        self.forever = forever
        self.keep_front = keep_front
        self._last_activate = 0.0
        self.logfile = logfile
        self._last_logged = 0.0
        self.role = role
        self.log_lines: collections.deque[str] = _EventLog(maxlen=8, path=(str(logfile) + ".events") if logfile else None)
        self.ctl = Controller(dry_run=dry_run, log=self._log_action)
        self.screen = Screen()
        self.kb = keybinds.load()
        self.riot = RiotLiveClient()
        self.brain = Brain()
        self.decision: Decision | None = None
        self.jungle_state: JungleState | None = None
        self.state: dict = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.mech: Mechanics | None = None
        self.hp = HpTracker(config.TIMING.damage_window_s)
        self.phase = "base"  # base | lane | dead
        self.intent = "farm"
        self.recorder = FixtureRecorder()
        self.guards = Guards()
        self._base_shop_done = False
        self.paused = False
        self._last_sig = (None, None, 0)
        self._gold_hist: collections.deque[tuple[float, float, int]] = collections.deque()
        self.mm: MinimapReader | None = MinimapReader(self.screen) if (config.GEOMETRY.minimap and not dry_run) else None
        self.mm_filter = PosFilter()
        self.tower_watch = TowerWatch()
        self._towers_seen_dead: collections.deque = collections.deque()   # (team, index) from the minimap icons
        self._tower_checked = 0.0
        self.whereabouts = Whereabouts()   # each enemy's last sighting on the minimap (fog.py)
        self.icon_matcher: IconMatcher | None = None
        self.mm_state: MinimapState | None = None
        self.side = "ORDER"
        self.dead_enemy_mid_towers: set[int] = set()
        # Fast path: perception thread (screen), API thread, tactical Jev thread, 30 Hz actor.
        self.vision: VisionReader | None = VisionReader() if (config.GEOMETRY.minimap and not dry_run) else None
        self.view: View | None = None
        self.perceive_fps = 0.0
        self.min_tracker = UnitTracker()
        self.champ_tracker = UnitTracker(max_jump_px=90, blink_px=480 * config.VISION.px_per_unit)
        self.tactic_hz = tactic_hz
        self.explore = explore
        self.champion_override = champion
        self.role_override = role
        self.kit: Kit = kit_for(champion or "yasuo", role)
        self.lane: Lane = Lane("mid", "ORDER")
        self.dlog = DecisionLog(decision_log if not dry_run else None)
        self._wake_actor = threading.Event()   # set on every new frame and every new Jev answer
        self._potion_at = 0.0
        self._level_seen, self._level_changed_at = 0, 0.0
        self.save_frames_s = save_frames_s
        self.frames_dir = frames_dir
        self.capture_backend = capture
        self.capture_fps = capture_fps
        self.capture_name = "-"
        self.perceive_ms = 0.0
        self.frame_age_ms = 0.0
        self._last_logged_seq = 0
        self.tactics: TacticalBrain | None = None
        self.fights: FightBrain | None = None
        self._self_trail: collections.deque[tuple[float, float]] = collections.deque(maxlen=60)  # (t, hp%) every 0.1 s
        self.enemy_kn = EnemyKnowledge()        # her spells: ranges, cooldowns, what she just used
        self._enemy_names: list[str] = []       # enemy team champion names (for reading the bar labels)
        self._name_marks: collections.deque = collections.deque(maxlen=40)  # (t, x, y, name) from the bar labels
        self._name_tried = 0.0
        self._seen_names: collections.Counter = collections.Counter()
        self.micro: Micro | None = None
        self.scene = None
        self.data: dict | None = None
        self.api_ms = 0.0
        self._api_dead = False
        self._last_table = 0.0
        # Itemization head: Data Dragon catalog + Jev need/next_item questions.
        try:
            self.shop_brain: ShopBrain | None = ShopBrain()
        except Exception as e:  # noqa: BLE001  no network and no cache: fall back to Doran's Blade only
            self.shop_brain = None
            self.log_lines.append(f"item catalog unavailable: {e}")
        self.build: BuildPlan | None = None
        self._build_wake = threading.Event()

    def _log_action(self, msg: str) -> None:
        self.log_lines.append(msg)

    # -- brain thread -------------------------------------------------------------------
    def _brain_loop(self) -> None:
        """Ask Jev once per period, or immediately when the tick flags a significant change."""
        period = 1 / config.TIMING.brain_hz
        while not self._stop.is_set():
            t0 = time.time()
            st = self.state
            if st and not self.paused:
                try:
                    self.decision = self.brain.decide(st, role_text=self.kit.role_text(self.lane.name), support=self.kit.support)
                    self.recorder.write(st, {"decision": self.decision.summary()})
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"brain error: {e}")
                    if credit_wait(e) > 1:
                        self._stop.wait(credit_wait(e))
            self._wake.wait(timeout=max(0.05, period - (time.time() - t0)))
            self._wake.clear()

    def _build_loop(self) -> None:
        """Re-plan the build every 20 s, and right away when the tick asks (entering base, death)."""
        while not self._stop.is_set():
            data = self.data
            if data and self.shop_brain is not None and not self.paused:
                try:
                    self.build = self.shop_brain.decide(data, self.side)
                    self.log_lines.append(self.build.summary())
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"build error: {e}")
                    if credit_wait(e) > 1:
                        self._stop.wait(credit_wait(e))
            self._build_wake.wait(timeout=20.0)
            self._build_wake.clear()

    def _shopping_state(self, data: dict) -> dict | None:
        b = self.build
        if b is None or self.shop_brain is None:
            return None
        cat = self.shop_brain.catalog
        return {
            "building_toward": b.target,
            "can_buy_now": [{"item": n, "price": cat.get(n).price if cat.get(n) else None} for n in b.buy_now],
            "gold": round(float(data.get("activePlayer", {}).get("currentGold", 0.0))),
            "enemy_needs": b.needs,
        }

    def _perceive_loop(self) -> None:
        """Reads every captured frame's health bars and HUD icons, and wakes the actor at once.
        ScreenCaptureKit pushes frames as the display refreshes; mss is the fallback. Everything no
        order waits for (the minimap, names, dialogs, saved frames) runs beside it on the newest frame
        (_slow_perceive_loop): the name OCR (~25 ms) used to hold the very frame a fight started on."""
        from jev.capture import open_capture

        cap = open_capture(self.screen.px_w, self.screen.px_h, prefer=self.capture_backend, fps=self.capture_fps)
        self.capture_name = cap.name
        excl = getattr(cap, "excluding", None)
        self.log_lines.append(f"capture: {cap.name}" + ("" if excl is None else f" (overlay left out: {excl})"))
        seq, n, t_rate = 0, 0, time.time()
        read_ms: collections.deque[float] = collections.deque(maxlen=120)
        self._frame_cv = threading.Condition()
        self._latest_frame = None
        threading.Thread(target=self._slow_perceive_loop, daemon=True).start()
        try:
            while not self._stop.is_set():
                f = cap.wait(seq, 0.2)
                if f is None:
                    continue
                if excl is False and getattr(cap, "excluding", False):
                    excl = True
                    self.log_lines.append("capture: the overlay is now left out of the stream")
                seq = f.seq
                if self.paused:
                    continue
                try:
                    t0 = time.perf_counter()
                    if self.vision is not None:
                        v = self.vision.read(f.img)
                        v.ts = f.ts  # latency is measured from when the frame was captured
                        self.view = v
                    read_ms.append((time.perf_counter() - t0) * 1000)
                    self.perceive_ms = sorted(read_ms)[len(read_ms) // 2]
                    self.frame_age_ms = (time.time() - f.ts) * 1000
                    self._wake_actor.set()
                    with self._frame_cv:
                        self._latest_frame = (f, self.view)
                        self._frame_cv.notify()
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"perception error: {e}")
                    time.sleep(0.05)
                n += 1
                if time.time() - t_rate >= 2.0:
                    self.perceive_fps = n / (time.time() - t_rate)
                    n, t_rate = 0, time.time()
        finally:
            cap.stop()

    def _slow_perceive_loop(self) -> None:
        """The minimap at most every 33 ms (macro positions, towers, which enemy icon is which), the
        name above a new champion's bar, game dialogs, and saved frames: on the newest frame, never
        in the way of the bar reader and the actor."""
        x0, y0, side = config.GEOMETRY.minimap
        done, last_mm, last_id = 0, 0.0, 0.0
        while not self._stop.is_set():
            with self._frame_cv:
                if self._latest_frame is None or self._latest_frame[0].seq == done:
                    self._frame_cv.wait(0.2)
                item = self._latest_frame
            if item is None or item[0].seq == done:
                continue
            f, v = item
            done = f.seq
            frame = f.img
            try:
                if self.mm is not None and f.ts - last_mm >= 0.033:
                    last_mm = f.ts
                    crop = frame[y0:y0 + side, x0:x0 + side]
                    st = self.mm.read(crop)
                    st.ts = f.ts
                    self.mm_state = self.mm_filter.apply(st)
                    if st.self_pos is not None and f.ts - self._tower_checked >= 2.0:
                        self._tower_checked = f.ts   # (the box found: the minimap is on screen)
                        ours = list(st.ally_champions) + [st.self_pos]
                        blue, red = (ours, list(st.enemy_champions)) if self.side == "ORDER" else (list(st.enemy_champions), ours)
                        for key in self.tower_watch.update(crop, self.mm, blue, red):
                            self._towers_seen_dead.append(key)
                    if st.self_pos is not None and st.enemy_champions and f.ts - last_id >= 0.25:
                        last_id = f.ts
                        self._name_icons(crop, st, f.ts)
                if v is not None:
                    self._read_names(frame, v, f.ts)
                if f.ts - getattr(self, "_dialog_checked", 0.0) >= 1.5:
                    self._dialog_checked = f.ts
                    self._dialog_ok = self._find_dialog_ok(frame)
                    self._shop_seen = self._shop_panel_open(frame)
                if self.save_frames_s:
                    self._save_frame(f, frame, v)
            except Exception as e:  # noqa: BLE001
                self.log_lines.append(f"perception error (minimap/names): {e}")
                time.sleep(0.05)

    def _name_icons(self, crop, st, ts: float) -> None:
        """Which enemy is which icon on the minimap, into the whereabouts (fog.py). The matcher
        downloads the five champions' art once, on its own thread."""
        m = getattr(self, "icon_matcher", None)
        if m is None:
            if self._enemy_names and not getattr(self, "_icon_matcher_started", False):
                self._icon_matcher_started = True
                names = list(self._enemy_names)
                threading.Thread(target=lambda: setattr(self, "icon_matcher", IconMatcher(names)), daemon=True).start()
            return
        cents = [self.mm.map_to_px(*p) for p in st.enemy_champions]
        ids = m.identify(crop, cents)
        self.whereabouts.update(ts, [(n, p) for n, p in zip(ids, st.enemy_champions) if n])

    def _save_frame(self, f, frame, v) -> None:
        """Every N seconds, and 4 per second while an enemy champion is on screen (fights are what
        reviews look at), 10 a second while a last-hit auto is watched. JPEG keeps a night small."""
        fighting = v is not None and (bool(v.enemies("champion")) or bool(v.enemies("monster")))
        gap = min(self.save_frames_s, 0.25) if fighting else self.save_frames_s
        if self.micro is not None and f.ts < getattr(self.micro, "capture_until", 0.0):
            gap = 0.1  # a last-hit auto being watched: its swing and whether the minion dies
        if f.ts - getattr(self, "_last_saved", 0.0) < gap:
            return
        import cv2
        from pathlib import Path

        self._last_saved = f.ts
        d = Path(self.frames_dir)
        d.mkdir(parents=True, exist_ok=True)
        gt = float(((self.data or {}).get("gameData") or {}).get("gameTime", 0.0))
        name = f"{time.strftime('%H%M%S')}{int(f.ts * 1000) % 1000:03d}_t{int(gt // 60):02d}{int(gt % 60):02d}"
        if f.ts - getattr(self, "_last_png", 0.0) >= 10.0:
            # Lossless now and then: vision thresholds do not survive JPEG.
            self._last_png = f.ts
            cv2.imwrite(str(d / f"{name}.png"), frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        else:
            cv2.imwrite(str(d / f"{name}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if v is not None:
            # What vision saw on this frame, for review next to the image.
            u2 = lambda u: [u.kind, u.team, int(u.x), int(u.y), round(u.hp, 2), list(u.bar)]
            rec = {"f": name, "t": round(gt, 1), "me": u2(v.me) if v.me else None,
                   "units": [u2(u) for u in v.units], "mode": self.micro.mode if self.micro else None,
                   "act": self.micro.last_action if self.micro else None}
            with open(d / "vision.jsonl", "a") as fh:
                fh.write(json.dumps(rec) + "\n")

    def _find_dialog_ok(self, frame) -> tuple[float, float] | None:
        """The Ok button of a modal game dialog (AFK Warning, Network Warning), in frame px, or
        None. Template match in a small box around the screen centre, about 1 ms."""
        import cv2
        from pathlib import Path

        if not hasattr(self, "_ok_tpl"):
            t = cv2.imread(str(Path(__file__).parent / "assets" / "dialog_ok.png"))
            self._ok_tpl = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY) if t is not None else None
        if self._ok_tpl is None:
            return None
        x0, y0 = 680, 430
        box = frame[y0:620, x0:1050]
        g = cv2.cvtColor(box, cv2.COLOR_BGRA2GRAY if box.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        r = cv2.matchTemplate(g, self._ok_tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx < 0.8:
            return None
        h, w = self._ok_tpl.shape[:2]
        return x0 + loc[0] + w / 2, y0 + loc[1] + h / 2

    def _shop_panel_open(self, frame) -> bool:
        """The shop is on screen (its tab bar or its SELL / UNDO buttons), or its corner anywhere
        (a dragged panel; its X is then clicked where it is, not where it belongs)."""
        from jev.uiscan import shop_corner, shop_visible

        if shop_visible(frame):
            self._shop_x_at = None
            return True
        x, y, score = shop_corner(frame)
        self._shop_x_at = (x + 24, y + 18) if score >= 0.8 else None
        return score >= 0.8

    def _close_stray_shop(self, now: float) -> bool:
        """A shop left open outside a purchase swallows every click and the level-up keys (Lee Sin
        stood in the jungle with it open until the AFK warning, game 3): close it with its X."""
        if not getattr(self, "_shop_seen", False) or getattr(self, "_shopping", False):
            self._shop_open_since = None
            return False
        if getattr(self, "_shop_open_since", None) is None:
            self._shop_open_since = now
        if now - self._shop_open_since < 2.0 or now - getattr(self, "_shop_closed_t", 0.0) < 3.0:
            return False
        self._shop_closed_t = now
        g = config.GEOMETRY
        with self.ctl.slow():
            x_at = getattr(self, "_shop_x_at", None) or g.shop_close
            if x_at:
                self.ctl.click(*self.screen.to_points(*x_at), "left")
            else:
                self.ctl.press(self.kb.shop)
        self._shop_seen = False
        self.log_lines.append("shop: closed a shop left open")
        return True

    def _dismiss_dialog(self, now: float) -> bool:
        pt = getattr(self, "_dialog_ok", None)
        if pt is None or now - getattr(self, "_dialog_clicked", 0.0) < 3.0:
            return False
        self._dialog_clicked = now
        self._dialog_ok = None
        with self.ctl.slow():
            self.ctl.click(*self.screen.to_points(*pt), "left")
        self.log_lines.append("dialog: clicked Ok (AFK / network warning)")
        return True

    def _api_loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            d = self.riot.all_game_data()
            self.api_ms = (time.time() - t0) * 1000
            if d is None:
                fails += 1
                # Dead only after ~10 s of failures with the game gone: the API stalls for a few
                # polls while the game finishes loading, and that ended the harness at 0:00.
                if fails >= 60 and not self.riot.is_game_running():
                    self._api_dead = True
            else:
                fails = 0
                self.data = d
            time.sleep(max(0.0, 0.1 - (time.time() - t0)))

    def _full_state(self, data: dict, perception: Perception) -> dict:
        if perception.lane_progress_pct is not None and perception.position in ("lane", "traveling"):
            perception.where_label = self.lane.where_label(perception.lane_progress_pct / 100)
        st = build_state(data, perception, self.role, self._opp_hint())
        if self.shop_brain is not None:
            st["enemy_lineup"] = [{k: e[k] for k in ("champion", "class", "damage", "level", "kda")}
                                  for e in enemy_team(data, self.shop_brain.catalog, self.side)]
        shopping = self._shopping_state(data)
        if shopping:
            st["shopping"] = shopping
        mm = self.mm_state
        me_pos = mm.pos if mm is not None else None
        enemies = list(mm.enemy_champions) if mm is not None else []
        allies = list(mm.ally_champions) if mm is not None else []
        st["map_places"] = map_places.describe(self.side, me_pos, enemies, allies, st.get("objectives", {}))
        sit = getattr(self, "situation", None)
        if sit is not None:
            st["situation"] = sit.summary()  # alive/dead with timers, towers, unseen enemies, the window
        if self.whereabouts.seen:
            alive = {str(p.get("championName")) for p in data.get("allPlayers", []) if not p.get("isDead")}
            st["enemy_whereabouts"] = self.whereabouts.describe(me_pos, time.time(), map_places.places(self.side), alive)
        st["map"] = {
            "i_am_near": map_places.nearest(me_pos, self.side) if me_pos else "unknown",
            "my_lane": self.lane.name,
            "enemy_champions_seen_near": [map_places.nearest(e, self.side) for e in enemies],
            "allied_champions_near": [map_places.nearest(a, self.side) for a in allies],
        }
        return st

    def overlay_data(self) -> dict:
        """Everything the overlay shows, as plain data (read from the main thread)."""
        now = time.time()
        st = self.state or {}
        me, g = st.get("me", {}), st.get("game", {})
        out: dict = {"header": {
            "title": f"{self.kit.name} {'support' if self.kit.support else self.kit.role.lower()} · {self.lane.name} lane"
                     if self.mech else "waiting for a game",
            "stats": (f"{g.get('time', '-')}  L{me.get('level', '-')}  HP {me.get('hp_percent', '-')}%  gold {me.get('gold', '-')}  "
                      f"cs {me.get('cs', '-')}  {me.get('kda', '')}") if me else "",
            "input": "dry run" if self.dry_run else ("live" if self.ctl.keys_ok() else "paused"),
        }}
        byp = lambda d, n: sorted(d.items(), key=lambda kv: -kv[1])[:n]
        t = self.tactics.tactic if self.tactics else None
        ex = self.micro.last_exec if self.micro else None
        if t is not None and now - t.state_ts < 3.0:
            units = t.state.get("visible_units", {})
            out["tactics"] = {
                "action": t.action, "p": t.probabilities.get(t.action, t.confidence), "explored": t.explored,
                "fits": t.menu_fits, "latency": t.latency_ms, "rate": self.tactics.rate() if self.tactics else 0.0,
                "menu": sorted(((n, t.probabilities.get(n, 0.0)) for n in t.menu), key=lambda kv: -kv[1]),
                "target": [(l, p, units.get(l, "")) for l, p in byp(t.target_probs, 3)],
                "where": [(w, p) for w, p in byp(t.where_probs, 3)],
                "distance": t.distance,
                "executed": ex.get("what") if ex else None, "exec_age": (now - ex["ts"]) if ex else 0.0,
            }
        d = self.decision
        if d is not None:
            out["strategy"] = {
                "intent": self.intent, "jev_intent": d.intent, "p": d.intent_confidence, "latency": d.latency_ms,
                "probs": byp(d.intent_probabilities, 4), "danger": d.danger, "aggr": d.aggression,
                "recall": d.should_recall, "fight": d.fight_favorable,
                "destination": byp(d.destination_probs, 3), "level_up": d.level_up,
            }
        b = self.build
        if b is not None:
            short = {"need_armor": "armor", "need_magic_resist": "mr", "need_tenacity": "tenacity",
                     "need_antiheal": "antiheal", "need_defense_first": "defense"}
            out["build"] = {"target": b.target, "p": b.confidence, "buy_now": b.buy_now,
                            "needs": {short.get(k, k): v for k, v in b.needs.items()}}
        react = {}
        if self.micro is not None:
            for kind in ("reflex", "lasthit", "jev"):
                lat = self.micro.latency(kind)
                if lat:
                    react[kind] = lat
        out["perf"] = {"apm": self.ctl.apm(), "capture": "sck" if self.capture_name == "screencapturekit" else self.capture_name,
                       "fps": self.perceive_fps, "read_ms": self.perceive_ms, "api_ms": self.api_ms, "react": react}
        fr = self.fights.read if self.fights is not None else None
        if fr is not None and now - fr.ts < 4.0:
            out["fight"] = {"plan": fr.plan, "p": fr.plan_probs.get(fr.plan, 0.0), "win": fr.win_all_in, "trade": fr.trade_worth,
                            "danger": fr.in_danger, "gank": fr.gank_coming, "latency": fr.latency_ms, "age": now - fr.ts,
                            "rate": self.fights.rate(), "focus": fr.focus}
        mi = self.micro
        if mi is not None:
            # What the frame-rate layer is doing: its mode (farm / trade / all_in / back_off) and the
            # last order it sent (combo steps like "E+Q onto the champion" show up here).
            out["micro"] = {"mode": mi.mode, "order": mi.last_action, "age": now - getattr(mi, "last_action_t", now)}
        out["log"] = list(self.log_lines)[-3:]
        sc = self.scene
        if sc is not None:
            pts = self.screen.to_points
            mk: dict = {"me": pts(*sc.me_xy), "killable": [pts(k.unit.x, k.unit.y) for k in sc.killable_auto]}
            if sc.champ is not None:
                opp = (st.get("lane_opponent") or {}).get("champion") or "enemy"
                mk["champ"] = (*pts(sc.champ.unit.x, sc.champ.unit.y), sc.champ.unit.hp * 100, opp)
            if ex:
                mk["exec"] = {"name": ex.get("what", ""), "target": ex.get("target"), "point": ex.get("point"), "age": now - ex["ts"]}
            out["markers"] = mk
        return out

    def fast_summary(self) -> list[str]:
        """Overlay lines for the fast path."""
        out = []
        t = self.tactics.tactic if self.tactics else None
        rate = self.tactics.rate() if self.tactics else 0.0
        if t is not None:
            probs = sorted(t.probabilities.items(), key=lambda kv: -kv[1])[:4]
            ex = t.extra
            tail = " ".join(f"{k}={v if not isinstance(v, float) else round(v, 1)}" for k, v in ex.items() if v is not None)
            out.append(f"tactic -> {t.action.upper()}{' (explore)' if t.explored else ''} p={t.confidence:.2f} fits={t.menu_fits:.2f} "
                       f"({t.latency_ms:.0f} ms, {rate:.1f}/s, {len(t.menu)} moves) {tail}")
            out.append("  " + "  ".join(f"{k} {v:.2f}" for k, v in probs))
        sc = self.scene
        if sc is not None:
            ch = f"champ {int(sc.champ.unit.hp * 100)}% @{int(sc.champ_dist or 0)}u" if sc.champ else "no champ"
            rdy = "".join(k for k in "QWER" if sc.ready.get(k))
            out.append(f"screen: {len(sc.minions)} minions ({len(sc.killable_auto)} killable), {sc.allies} ally, {ch}, ready {rdy or '-'}"
                       + (" Q3" if hasattr(self.kit, "q") and self.kit.q.q3(time.time()) else ""))
        dd = self.decision
        if dd is not None and (dd.destination or dd.level_up):
            top = sorted(dd.destination_probs.items(), key=lambda kv: -kv[1])[:2]
            out.append(f"map: {' '.join(f'{k} {v:.2f}' for k, v in top)}" + (f"   level-up pick: {dd.level_up}" if dd.level_up else ""))
        b = self.build
        if b is not None:
            needs = " ".join(f"{k.replace('need_', '')[:7]} {v:.2f}" for k, v in b.needs.items())
            out.append(f"build -> {b.target} p={b.confidence:.2f} | buy now: {', '.join(b.buy_now) or '-'}")
            out.append(f"  needs: {needs}")
        if self.micro is not None:
            parts = []
            for kind in ("reflex", "lasthit", "jev"):
                lat = self.micro.latency(kind)
                if lat:
                    parts.append(f"{kind} {lat[0]:.0f}/{lat[1]:.0f}")
            if parts:
                out.append("screen->input ms (p50/p90): " + "  ".join(parts))
        out.append(f"APM {self.ctl.apm()}   {self.capture_name} {self.perceive_fps:.0f} fps, read {self.perceive_ms:.1f} ms   api {self.api_ms:.0f} ms")
        out.append(f"micro: {self.micro.last_action if self.micro else '-'}")
        return out

    def _wave_fwd(self, view, enemy_minions, world) -> tuple[float, float] | None:
        """Screen direction up the lane where the minions are. Top and bot lanes turn 90 degrees
        at the corner: Yasuo held back on the vertical part while the wave fought on the horizontal
        one (g28/g29 top), and the lane read at his own spot pointed "up" across a wave lined up
        left to right, which scrambled the hold-back spot and the melee/caster order. Lane
        direction at the wave's centre agreed with the wave's own axis twice as well (median off
        32 -> 17 degrees on g28/g29 frames)."""
        if self.mech is None:
            return None
        prog = self.mech.nav.progress
        units = list(enemy_minions) + view.allies("minion")
        if world is not None and units:
            pts = [world(u) for u in units]
            at, off = self.lane.project((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
            if off < 1200:
                prog = at
        return self.lane.screen_dir(prog)

    def _micro_step(self, data: dict, ap: dict, stats: dict, now: float, standing: bool = True,
                    camp_pt: tuple[float, float] | None = None, camp_big: bool = False, escaping: bool = False) -> bool:
        """Screen-level play while units are on screen: Jev's tactical choice, the kit's reflexes,
        and (when `standing`) the kit's standing behaviour. Returns False when nothing was done
        this tick (macro movement takes over)."""
        view, mi, kit = self.view, self.micro, self.kit
        if mi is not None and mi.run_due(now):
            return True  # a combo step fired this tick
        if view is None or mi is None or now - view.ts > 0.3:
            return False
        raw_minions = view.enemies("minion")
        mm = self.mm_state
        ppu = config.VISION.px_per_unit
        world = (lambda u: (mm.pos[0] + (u.x - view.me.x) / ppu, mm.pos[1] - (u.y - view.me.y) / ppu)) \
            if (mm is not None and mm.pos is not None and view.me is not None) else None
        if camp_pt is not None:
            # Clearing a camp: red units (small monsters look like minions) and large monsters near it.
            cand = raw_minions + view.enemies("monster")
            raw_minions = [u for u in cand if world is None or math.dist(world(u), camp_pt) < 1000]
        elif world is not None:
            # Jungle monsters have red bars like enemy minions: keep only units near the lane path.
            raw_minions = [u for u in raw_minions if self.lane.project(world(u))[1] < 900]
        # A minion seen in 3 reads (~60 ms): a one-frame read of scenery drew a Q and an E "inside their wave"
        # in our own jungle (g42 26:06, after respawn), and Q stacks and cooldowns went to nothing.
        minions = [t for t in self.min_tracker.update(raw_minions, now) if len(t.hist) >= 3]
        self._audit_lasthits(mi, now)
        mi.score_skills(now)
        fwd = self._wave_fwd(view, raw_minions, world if camp_pt is None else None)
        mi.dodge_lines = self._opponent_throws_lines()
        champs = self.champ_tracker.update(view.enemies("champion"), now)
        self._name_tracks(champs, now)
        self._note_blinks(champs, now)
        if not minions and not champs and not (self.kit.support and view.allies("champion")):
            self.scene = None
            return False
        ad = float(stats.get("attackDamage", 60.0))
        aspd = float(stats.get("attackSpeed", 0.7))
        q_rank = int(ap.get("abilities", {}).get("Q", {}).get("abilityLevel", 0))
        game_s = float((data.get("gameData") or {}).get("gameTime", 0.0))
        mi.ad, mi.aspd = ad, aspd
        e_rank = int(ap.get("abilities", {}).get("E", {}).get("abilityLevel", 0))
        sc = build_scene(view, minions, champs, ad, q_rank, game_s, now, config.GEOMETRY.champion_px,
                         aspd=aspd, move_speed=float(stats.get("moveSpeed", 345.0)),
                         fwd=fwd, e_dmg=kit.e_minion_damage(e_rank, ad, int(ap.get("level", 1))))
        mi.dash_ok = not getattr(self, "_near_enemy_tower", False)
        sc.lane = self._lane_info(view, stats, sc, now)
        sc.ctx = self._fight_ctx(data, ap, stats, sc)
        lr = getattr(mi, "learner", None)
        if lr is not None:
            lr.opponent = str(sc.lane.get("opp") or "").lower()
            her = sc.champ
            reach = float(sc.lane.get("opp_reach", float(sc.lane.get("opp_range", 550.0)) + 150.0))
            lr.exposure(now, inp_hp(data), None if her is None else sc.dist(her) < reach)
            lr.tick(now, inp_hp(data), her.unit.hp if her is not None else None)
        if kit.support:
            sc.killable_auto = []  # supports leave last hits to the carry
        self.scene = sc
        mi.fwd = fwd or mi.fwd
        d = self.decision
        plan = {
            "intent": self.intent,
            "lane": self.lane.name,
            "aggression_0_to_2": round(d.aggression, 1) if d else 1.0,
            "danger": round(d.danger, 1) if d else 0.0,
            "fight_favorable": round(d.fight_favorable, 2) if d else 0.5,
        }
        me_p = find_me(data) or {}
        ctx = actions.Ctx(mi=mi, sc=sc, kit=kit, lane=self.lane, now=now, aspd=aspd,
                          ally_units=view.allies("champion"), lane_progress=self.mech.nav.progress if self.mech else 0.5)
        inp = TacticInput(
            ctx=ctx, api=self.state, plan=plan, ts=view.ts,
            summoners=actions.summoner_names(me_p), items=list(me_p.get("items") or []),
            items_ready=dict(view.hud.items_ready), hp_pct=float((self.state.get("me") or {}).get("hp_percent") or 100),
            potion_used_at=self._potion_at, opp_name=((self.state.get("lane_opponent") or {}).get("champion") or "enemy"),
        )
        if self.tactics is not None:
            self.tactics.publish(inp)
        if self.fights is not None and (sc.champ is not None or self._enemies_near_on_map(2500)):
            try:
                self.fights.publish(self._fight_features(sc, champs, now, data), view.ts)
            except Exception as e:  # noqa: BLE001  a features bug must not stop the micro layer
                self.log_lines.append(f"fight features error: {e}")
        mi.summoners, mi.hp_pct = inp.summoners, inp.hp_pct
        if hasattr(kit, "q") and hasattr(kit.q, "hud"):
            kit.q.hud = view.hud.q3
            mi.q3_hint = bool(kit.q.q3(now))
        mi.scene = sc
        mi.q_notes_on = hasattr(kit, "q") and hasattr(kit, "tornado_at")   # Yasuo: count units hit per Q
        if hasattr(kit, "r_rank"):
            kit.r_rank = int(ap.get("abilities", {}).get("R", {}).get("abilityLevel", 0))
        mi.hp_lost = getattr(self, "_hp_lost", 0.0)  # HP% lost in the damage window
        self._note_last_champ(sc, mi, now)
        if self.kit.execute_ok and mi._can_order(now) and self._execute(kit, mi, sc, now):
            mi.reacted("reflex", view.ts)
            return self._took(sc, "execute", True)
        if escaping and self._fight_back(sc, mi, now):
            escaping = False   # one of them on me and I am not behind: turning my back loses (g40)
        if escaping:
            # Walking out: no fight entries; reflexes only (Flash, defensive summoners, potion,
            # the kit's escape dash). Retreats used to skip this step entirely, so a Yasuo taking
            # 80% in four seconds walked to his death with Flash up (game 4).
            if mi.mode in FIGHT_MODES:
                mi.set_mode("back_off", now)
            if not mi._can_order(now):
                return self._took(sc, "retreating", False)
            if self._escape_reflex(ctx, inp, now):
                mi.reacted("reflex", view.ts)
                return self._took(sc, "retreating", True)
            fx, fy = self.lane.screen_dir(self.mech.nav.progress) if self.mech else mi.fwd
            return self._took(sc, "retreating", bool(kit.escape(mi, sc, now, (-fx, -fy))))
        if sc.champ is None and mi._can_order(now) and self._hunt(sc, mi, now):
            return self._took(sc, "hunt", True)
        self._fight_triggers(sc, mi, now)
        if not mi._can_order(now):
            return self._took(sc, "order rate limit", True)
        if camp_pt is not None and camp_big and self._smite_reflex(ctx, inp, now):
            mi.reacted("reflex", view.ts)
            return True
        if self._escape_reflex(ctx, inp, now):
            mi.reacted("reflex", view.ts)
            return True
        if self._potion_reflex(inp, now):
            return self._took(sc, "potion", True)
        if self._ward_reflex(sc, inp, view, now):
            return self._took(sc, "ward", True)
        if kit.reflex(mi, sc, now, plan):
            mi.reacted("reflex", view.ts)
            return self._took(sc, f"kit reflex: {mi.last_action[:24]}", True)
        t = self.tactics.tactic if self.tactics else None
        if t is not None and t.seq > mi.last_seq and t.age(now) < config.FAST.tactic_stale_s:
            mi.last_seq = t.seq
            spec = {s.name: s for s in menu(inp)}.get(t.action)  # still possible right now?
            executed = False
            if spec is not None:
                what = actions.execute(ctx, spec, t.cands, t.target_probs, t.where_probs, t.distance)
                executed = bool(what) and spec.mode is None
                if executed:
                    mi.reacted("jev", t.state_ts)
                    if spec.name.startswith("potion"):
                        self._potion_at = now
            self.dlog.record(t, executed, self._metrics(), str(self.state.get("game", {}).get("time")))
            if executed:
                return True
        if not standing and mi.mode not in FIGHT_MODES:
            return self._took(sc, "traveling", False)
        before = mi.orders
        if sc.champ is not None:
            self._champ_seen_t = now
        # Free push: their champion has not been on screen for 3 s, our wave is here, I am healthy and
        # not under their tower. Waiting for exact last hits paid 2.3 CS a minute in g17 (killable
        # minions sat at 7-10% for a second); hitting the wave outright is more gold against bots.
        sit = getattr(self, "situation", None)
        push_free = ((self.intent == "farm" or now < getattr(self, "_shove_until", 0.0)) and self.jungle_state is None
                     and not kit.support and sc.champ is None
                     and now - getattr(self, "_champ_seen_t", 0.0) > 3.0 and mi.hp_pct >= 45 and len(sc.minions) >= 2
                     and sc.allies > 0 and not getattr(self, "_near_enemy_tower", False)
                     and (sit is None or sit.unseen < 4 or sit.power_play))  # (four unseen: a pushed wave is a gank)
        ok = kit.step(mi, sc, now, aspd, mi.mode, self.intent in ("push_tower", "objective") or push_free)
        if mi.orders > before and mi.last_action.startswith("last hit"):
            mi.reacted("lasthit", view.ts)
        return ok

    def _initiate(self, sc, mi, now: float, fr) -> bool:
        """A kit with its own go/no-go (Lee Sin: kits.LeeSin.go, from the guides) starts the fight itself:
        the fight head read Lee's fights as back_off 90% of the time (989 of 1,097 reads in g30) and he
        never initiated. Not when Jev reads me about to die; a fight already on is left alone."""
        go = getattr(self.kit, "go", None)
        if go is None:
            return False
        fresh_danger = fr is not None and fr.age(now) < 0.9 and fr.in_danger
        if mi.mode == "all_in" and now < getattr(self, "_committed_until", 0.0):
            # The combo plays out (the next back_off read would end it a tick after it started), unless
            # Jev reads me about to die, I am low, a second one of theirs came, or she is gone.
            if (sc.champ is None or mi.hp_pct < 25 or (fresh_danger or 0.0) >= 0.85
                    or sc.enemy_champs > len(sc.ally_champs) + 1 or self._her_team_near(sc)):
                self._committed_until = 0.0
                return False
            return True
        if sc.champ is None or mi.mode == "all_in":
            return False
        if (fresh_danger or 0.0) >= 0.75:
            return False
        ch = sc.champ
        ppu = config.VISION.px_per_unit
        on_her = sum(1 for a in sc.ally_champs if math.hypot(a.x - ch.unit.x, a.y - ch.unit.y) <= 450 * ppu)
        her_half = None
        mm = self.mm_state
        if mm is not None and mm.pos is not None:
            hx, hy = mm.pos[0] + (ch.unit.x - sc.me_xy[0]) / ppu, mm.pos[1] - (ch.unit.y - sc.me_xy[1]) / ppu
            ln = min((Lane(n, self.side) for n in ("top", "mid", "bot")), key=lambda l: l.project((hx, hy))[1])
            prog, off = ln.project((hx, hy))
            her_half = off < 1200 and prog <= ln.center
        if self._her_team_near(sc):
            return False
        sit = getattr(self, "situation", None)
        if sit is not None and sit.unseen >= 3 and her_half is False and ch.unit.hp >= 0.25 and not sit.power_play:
            return False   # into their half with three or more of them in the fog
        why = go(sc, mi, now, on_her, her_half)
        if why is None:
            return False
        if self.kit.under_their_tower(sc, ch.unit.x, ch.unit.y) and not self.kit.dive_ok(mi, sc):
            return False
        self._commit(ch, mi, now, f"initiate on {ch.unit.hp * 100:.0f}% at {sc.champ_dist or 0:.0f}u: {why}")
        self._committed_until = now + 5.0
        return True

    def _note_last_champ(self, sc, mi, now: float) -> None:
        """Where the champion in view is on the map and which way she walks (map units, units/s): the hunt
        follows her from it when she drops out of view."""
        ch, mm = sc.champ, self.mm_state
        if ch is None or mm is None or mm.pos is None:
            return
        ppu = config.VISION.px_per_unit
        vx, vy = ch.velocity(now, 0.35) if hasattr(ch, "velocity") else (0.0, 0.0)
        mvx, mvy = mi.self_velocity(now, sc.me_xy, sc.move_speed)
        self._last_champ = {"t": now, "hp": ch.unit.hp,
                            "pos": (mm.pos[0] + (ch.unit.x - sc.me_xy[0]) / ppu, mm.pos[1] - (ch.unit.y - sc.me_xy[1]) / ppu),
                            "v": ((vx + mvx) / ppu, -(vy + mvy) / ppu),
                            "towered": self.kit.under_their_tower(sc, ch.unit.x, ch.unit.y)}

    def _hunt(self, sc, mi, now: float) -> bool:
        """A champion at 20% or less just dropped out of view (a brush, the fog at the screen's edge): for 3 s
        walk where she is headed and Q there when it reaches (a skillshot hits in a brush). g42's Nasus
        at 2% walked out of Yasuo's view at 9:26 and lived; the chase had stopped with the last frame of
        her. Not under her tower, not into her team, not while I am under 40%."""
        lc, mm = getattr(self, "_last_champ", None), self.mm_state
        if lc is None or mm is None or mm.pos is None or sc.enemy_champs:
            return False
        dt = now - lc["t"]
        if not 0.15 < dt < 3.0 or lc["hp"] > 0.2 or lc["towered"] or mi.hp_pct < 40 or mi.mode == "back_off":
            return False
        if self._her_team_near_pt(lc["pos"]):
            return False
        k = min(dt + 0.3, 1.8)
        vx, vy = lc["v"]
        sp = math.hypot(vx, vy)
        if sp > 420:
            vx, vy = vx / sp * 420, vy / sp * 420
        tx, ty = lc["pos"][0] + vx * k, lc["pos"][1] + vy * k
        ppu = config.VISION.px_per_unit
        sx, sy = sc.me_xy[0] + (tx - mm.pos[0]) * ppu, sc.me_xy[1] - (ty - mm.pos[1]) * ppu
        if self.kit.under_their_tower(sc, sx, sy):
            return False
        d = math.hypot(sx - sc.me_xy[0], sy - sc.me_xy[1]) / ppu
        kit = self.kit
        if (sc.ready.get("Q") and hasattr(getattr(kit, "q", None), "q3")
                and d <= (config.VISION.q3_range * 0.85 if kit.q.q3(now) else config.VISION.q_range)
                and now - getattr(self, "_hunt_q_t", 0.0) > 1.0):
            was_q3 = kit.q.q3(now)
            mi.cast(1, sx, sy)
            kit.q.cast(True, now)
            self._hunt_q_t = now
            mi._ordered(now, f"hunt: {'Q3' if was_q3 else 'Q'} where she went ({lc['hp'] * 100:.0f}%, {dt:.1f} s ago)")
            return True
        mi.move_screen(sx, sy, now, f"hunt: after her ({lc['hp'] * 100:.0f}%, {dt:.1f} s ago, {d:.0f}u)", every=0.15)
        return True

    def _her_team_near_pt(self, her) -> bool:
        """Two or more of them within 1500 units of a map point, more than of us there (me counted)."""
        mm = self.mm_state
        foes = sum(1 for e in mm.enemy_champions if dist(e, her) < 1500)
        friends = 1 + sum(1 for a in mm.ally_champions if dist(a, her) < 1500)
        return foes >= 2 and foes > friends

    def _fight_back(self, sc, mi, now: float) -> bool:
        """One of them is on me, hitting (8%+ lost in the damage window), within 450 units, alone (no other
        champion of theirs on screen or by her on the minimap), and I am not behind (50%+, not under her)
        with a spell up: fight back, all in, and hold the heavy-damage retreat 3 s. g40's
        Yasuo turned and walked away from a single champion at 96% and at 100% and died from behind both
        times; walking away from a melee diver loses the race. Not when Jev reads me about to die."""
        ch = sc.champ
        if ch is None or sc.enemy_champs != 1 or self.kit.support:
            return False
        d = sc.champ_dist or 9e9
        lost = getattr(self, "_hp_lost", 0.0)
        # (50%+ and not behind her at all: at 92 vs 82, 53 vs 48 and 43 vs 44 against Garen the exchanges came
        # out even, both near 40%, and their jungler finished Yasuo, g41 5:43)
        if d > 450 or lost < 8 or mi.hp_pct < 50 or mi.hp_pct < ch.unit.hp * 100:
            return False
        raw = getattr(ch, "name", "") or self._her_name(sc) or ""
        name = raw.lower().replace(" ", "").replace(".", "")
        margin = 15 if name in getattr(self.kit, "HEALERS", ()) else 0
        # (Dr. Mundo heals through an even exchange: g45 fought back at 84/62, 70/60, 51/45 and bled.) One
        # who has killed me this game gets 10 points more per kill (g45: fed Mundo, four deaths to him).
        margin += min(30, 10 * macro.killed_by(self.data or {}, raw))
        if mi.hp_pct < ch.unit.hp * 100 + margin:
            return False
        if not (sc.ready.get("Q") or sc.ready.get("E") or sc.ready.get("R")):
            return False
        fr = self.fights.read if self.fights is not None else None
        if fr is not None and fr.age(now) < 0.9 and fr.in_danger >= 0.85:
            return False
        if self._her_team_near(sc) or self.kit.under_their_tower(sc, ch.unit.x, ch.unit.y):
            return False
        if mi.mode != "all_in":
            self._commit(ch, mi, now, f"she is on me ({ch.unit.hp * 100:.0f}% at {d:.0f}u, me {mi.hp_pct:.0f}%, "
                                      f"-{lost:.0f}%), fighting back")
        self.guards.fight_back_until = now + 3.0
        self.guards.retreat_until = min(self.guards.retreat_until, now)
        return True

    def _her_team_near(self, sc) -> bool:
        """Two or more of them within 1500 units of her on the minimap, more than of us there (me counted):
        the fight she leads me into. g39's Yasuo chased a 48% champion and re-engaged her twice this way
        and went 100% -> 30% in two seconds both times (deaths at 37:03 and 42:47)."""
        mm, ch = self.mm_state, sc.champ
        if mm is None or mm.pos is None or ch is None:
            return False
        ppu = config.VISION.px_per_unit
        her = (mm.pos[0] + (ch.unit.x - sc.me_xy[0]) / ppu, mm.pos[1] - (ch.unit.y - sc.me_xy[1]) / ppu)
        foes = sum(1 for e in mm.enemy_champions if dist(e, her) < 1500)
        friends = 1 + sum(1 for a in mm.ally_champions if dist(a, her) < 1500)
        return foes >= 2 and foes > friends

    def _gank_commit(self, sc, mi, now: float, fr) -> bool:
        """A jungler on a gank who sees his target within Sonic Wave reach goes in: all in on it, Q from
        range first. The fight head read ganks as back_off or poke (win 0.2 against a full-HP laner it
        cannot see our laner next to), and 32 ganks over g30, g32 and g36 made no kill: Lee walked to
        the lane, looked, and left. Not when Jev reads me about to die, when I am under 40%, into more
        of them than of us, or under their tower (unless she is nearly dead)."""
        if self.jungle_state is None or getattr(self, "_gank", None) is None or self.intent != "objective":
            return False
        ch = sc.champ
        if ch is None or (sc.champ_dist or 9e9) > getattr(self.kit, "Q_RANGE", 1200.0) + 100:
            return False
        if mi.hp_pct < 40 or sc.enemy_champs > len(sc.ally_champs) + 1:
            return False
        if fr is not None and fr.age(now) < 0.9 and fr.in_danger >= 0.75:
            return False
        if self.kit.under_their_tower(sc, ch.unit.x, ch.unit.y) and ch.unit.hp >= 0.3:
            return False
        if mi.mode != "all_in":
            self._commit(ch, mi, now, f"gank {self._gank['lane']}: on {ch.unit.hp * 100:.0f}% at {sc.champ_dist or 0:.0f}u "
                                      f"({len(sc.ally_champs)} of ours, {sc.enemy_champs} of theirs in view)")
        else:
            self._focus_id, self._focus_until = ch.id, now + 4.0
        return True

    def _fight_triggers(self, sc, mi, now: float) -> None:
        """Code-level entries and exits around Jev's fight modes: a kill window (enemy champion
        low and close, me healthy) goes all in at once; the strategy head's all_in intent does
        too; a fight I am clearly losing backs off. Supports keep Jev's choice."""
        if self.kit.support:
            return
        self._focus_target(sc, mi, now)
        fr = self.fights.read if self.fights is not None else None
        if self._fight_back(sc, mi, now) or self._initiate(sc, mi, now, fr) or self._gank_commit(sc, mi, now, fr):
            return
        if fr is not None and fr.age(now) < 0.9 and (sc.champ is not None or fr.plan in ("back_off", "escape")):
            self._apply_fight_read(fr, sc, mi, now)
            return
        ch, d = sc.champ, sc.champ_dist or 9e9
        # Within 650 units the minimap cannot confirm her (icons that close to ours are dropped as our
        # own portrait), and monsters are told apart by their gold frame now: trust vision. The check
        # dropped a real team fight at the river six times in two seconds (g18).
        if ch is not None and d > 900 and not self._champ_on_minimap(now):  # (900: past every entry's reach, or they flap)
            # A champion-sized bar with no enemy champion icon near us on the minimap is a monster
            # (the dragon read as a champion at 8% and got an all-in and an ignite, game 3).
            if mi.mode in FIGHT_MODES:
                mi.set_mode("farm", now)
                self.log_lines.append("fight: no enemy champion on the minimap near me, dropping the fight")
            return
        outnumbered = sc.enemy_champs >= 2 and not sc.ally_champs
        if mi.mode in ("trade", "poke") and ch is not None and not outnumbered:
            # A trade that worked becomes the kill: in g19 trades took champions from ~80% to 22-31%
            # while Yasuo lost next to nothing, then stepped back and they walked away.
            rec = [h for t, h in ch.hist if now - t <= 0.5]
            med = sorted(rec)[len(rec) // 2] if len(rec) >= 3 else None
            if med is not None and med < 0.45 and max(rec) < 0.6 and mi.hp_pct >= max(45.0, med * 100 + 20) and d < 900:
                self._commit(ch, mi, now, f"trade won ({med * 100:.0f}% vs me {mi.hp_pct:.0f}%), all in for the kill")
                mi.flash_in_ok = med < 0.25 and mi.hp_pct >= 40
                return
        if mi.mode in FIGHT_MODES:
            if ch is not None and mi.hp_pct < 25 and ch.unit.hp > mi.hp_pct / 100 + 0.15:
                mi.set_mode("back_off", now)
                self.log_lines.append(f"fight: losing ({mi.hp_pct:.0f}% vs {ch.unit.hp * 100:.0f}%), backing off")
            elif ch is not None and outnumbered and ch.unit.hp > 0.2:
                # A second champion joined (a gank): Fiddlesticks then Kayle took Yasuo 71% -> 13% in
                # a second while he chased (g08). Out at once, unless the target is nearly dead.
                mi.set_mode("back_off", now)
                self.log_lines.append(f"fight: outnumbered ({sc.enemy_champs} enemy champions, no ally), backing off")
            return
        if ch is not None and outnumbered and d < 900 and mi.mode != "back_off":
            mi.set_mode("back_off", now)
            self.log_lines.append(f"fight: {sc.enemy_champs} enemy champions close and no ally, backing off")
            return
        if ch is None or getattr(self, "_near_enemy_tower", False):
            return
        recent = [h for t, h in ch.hist if now - t <= 0.5]
        their = sorted(recent)[len(recent) // 2] if len(recent) >= 3 else None  # half-second median: one misread is not a window
        allies = len(sc.ally_champs)
        tower = self._own_tower_near(ch, sc)
        behind = self._levels_behind()
        if (tower is not None and their is not None and d < 650 and mi.hp_pct >= 30 and sc.enemy_champs <= allies + 1
                and mi.hp_pct >= their * 100 - 35 and mi.mode != "all_in" and (behind < 2 or their < 0.5)):
            # (Two levels behind their team, the tower does not make up for it: Lee at level 8 went all in
            # on a full enemy under our tower and died in five seconds, g21.)
            # She is inside our tower's range next to me: the tower shoots her once she hits me.
            # Intermediate bots dive and chase under towers; that fight is ours.
            self._commit(ch, mi, now, f"under our tower ({their * 100:.0f}% at {d:.0f}u, me {mi.hp_pct:.0f}%), all in")
            return
        ppu = config.VISION.px_per_unit
        ally_on_her = any(math.hypot(a.x - ch.unit.x, a.y - ch.unit.y) <= 450 * ppu for a in sc.ally_champs)
        if (allies and sc.enemy_champs <= allies + 1 and mi.hp_pct >= 40 and d < 800 and their is not None
                and (their < 0.5 or (allies >= sc.enemy_champs and mi.hp_pct >= 60 and ally_on_her))):
            # (Healthy targets only once an ally is on them: first in, Yasuo took the focus and spent 29%
            # of g22's late game dead.)
            # A team fight: allied champions on screen, numbers even or better. Bots engage all game;
            # Yasuo farmed next to their fights (0 kills in g09-g15). Join on the weakest in reach.
            self._commit(ch, mi, now, f"team fight ({allies + 1} vs {sc.enemy_champs}), all in on {their * 100:.0f}% at {d:.0f}u")
            return
        flash_down = self.enemy_flash_down(getattr(ch, "name", "") or "", now)
        if (their is not None and their < (0.55 if flash_down else 0.45) and max(recent) < 0.6
                and mi.hp_pct >= their * 100 + (15 if flash_down else 25) and d < 700
                and sc.enemy_champs == 1 and self.kit.minions_near_champ(sc) < 4 and mi.mode != "back_off"
                and not self._her_team_near(sc)):
            # Lane kill pressure: she is under 45% and I am well ahead in HP, with no full wave around her.
            self._commit(ch, mi, now, f"kill pressure ({their * 100:.0f}% vs me {mi.hp_pct:.0f}% at {d:.0f}u), all in")
            return
        if ((fr is None or fr.age(now) > 3.0) and sc.enemy_champs == 1 and mi.mode not in FIGHT_MODES
                and mi.mode != "back_off" and their is not None and now >= getattr(mi, "trade_cooldown_until", 0.0)
                and not self._her_team_near(sc)):
            # No read from Jev's fight head (this branch): out of API credits in g35 from 15:55, Xin Zhao
            # walked up on a full-HP Yasuo, hit first, and Yasuo walked away until he died (20:04). Two rules
            # stand in: the kit's own trade window, and fighting back when she is on me and I am not behind.
            window = self._kit_trade_window(sc, mi, now)
            if window:
                if window == "all_in":
                    self._commit(ch, mi, now, f"kit window (no Jev read), all in on {their * 100:.0f}% at {d:.0f}u")
                else:
                    mi.set_mode("trade", now)
                    self.log_lines.append(f"fight: kit trade window (no Jev read), {their * 100:.0f}% at {d:.0f}u, me {mi.hp_pct:.0f}%")
                return
        if (d < 700 and not sc.ready.get("Q") and not sc.ready.get("E") and mi.hp_pct < 80 and not sc.ally_champs
                and ch.unit.hp * 100 > mi.hp_pct + 10 and mi.mode not in ("back_off", "all_in")):
            # Nothing up to answer with and she is healthier: Lee kept hitting a camp while a full-HP
            # champion hit him from 200 units, 72% -> 0 in seven seconds (g23). Leave now.
            mi.set_mode("back_off", now)
            self.guards.retreat_until = max(self.guards.retreat_until, now + 3.0)
            self.log_lines.append(f"fight: Q and E down, she is healthier ({ch.unit.hp * 100:.0f}% vs me {mi.hp_pct:.0f}%), retreating")
            return
        if mi.hp_pct < 55 and ch.unit.hp * 100 > mi.hp_pct + 20 and d < 900 and mi.mode != "back_off":
            # Outmatched: half HP with a healthy champion walking up. Farming on cost the second
            # death of g06 (50% -> 0 in five seconds, Flash at 23% too late). Back off now.
            mi.set_mode("back_off", now)
            if mi.hp_pct < 40:
                # Low and outmatched: leave, do not just step back (43% -> 26% over a dozen seconds of
                # back_off steps, g15).
                self.guards.retreat_until = max(self.guards.retreat_until, now + 4.0)
            self.log_lines.append(f"fight: outmatched ({mi.hp_pct:.0f}% vs {ch.unit.hp * 100:.0f}% at {d:.0f}u), "
                                  f"{'retreating' if mi.hp_pct < 40 else 'backing off'}")
            return
        team_near = self._her_team_near(sc)
        steady_low = their is not None and their < (0.15 if team_near else 0.3) and max(recent) < 0.45
        # One low reading is not a kill window: an overlapped bar read Kayle at 15% while she had
        # 79%, and the all-in cost Yasuo 30% HP (game 4). The median of half a second must agree.
        # (With her team by her on the minimap, only when she is nearly dead.)
        if steady_low and d < 700 and mi.hp_pct > 35:
            self._commit(ch, mi, now, f"kill window ({ch.unit.hp * 100:.0f}% at {d:.0f}u), all in")
            mi.flash_in_ok = ch.unit.hp < 0.2 and mi.hp_pct > 40
            return
        mode = None if team_near else self._kit_trade_window(sc, mi, now)
        if mode:
            mi.set_mode(mode, now)
            self.log_lines.append(f"fight: trade window, {mode} ({ch.unit.hp * 100:.0f}% at {d:.0f}u, me {mi.hp_pct:.0f}%)")
            return
        dd = self.decision
        if (dd is not None and dd.intent == "all_in" and dd.intent_confidence >= 0.5 and d < 900 and mi.hp_pct > 40
                and now - dd.ts < 2.0 and mi.mode != "back_off"):
            mi.set_mode("all_in", now)
            self.log_lines.append(f"fight: strategy says all in (p={dd.intent_confidence:.2f})")

    def _item_stat(self, items: list, key: str) -> float:
        cat = self.shop_brain.catalog if self.shop_brain is not None else None
        if cat is None:
            return 0.0
        tot = 0.0
        for it in items or []:
            i = cat.get(str(it.get("displayName", "")))
            if i is not None:
                tot += float(i.stats.get(key, 0.0)) * max(1, int(it.get("count", 1) or 1))
        return tot

    def _fight_ctx(self, data: dict, ap: dict, stats: dict, sc) -> dict:
        """What a kit's damage math needs: my level, ability ranks, total and bonus AD (items: the
        live API gives only the total, and Data Dragon lists Lee's AD growth as 0), energy, and the
        target's level, HP pool, armor and magic resist (base stats at its level plus its items)."""
        me = find_me(data) or {}
        abil = ap.get("abilities", {}) or {}
        ctx = {"level": int(ap.get("level", 1) or 1),
               "ranks": {k: int((abil.get(k) or {}).get("abilityLevel", 0) or 0) for k in "QWER"},
               "ad": float(stats.get("attackDamage", 60.0)),
               "crit": float(stats.get("critChance", 0.0) or 0.0),
               "bonus_ad": self._item_stat(me.get("items") or [], "FlatPhysicalDamageMod"),
               "energy": float(stats.get("resourceValue") or 0.0) if str(stats.get("resourceType", "")).upper() == "ENERGY" else None,
               # the trinket (slot 7) is a ward and off cooldown: a ward hop is possible
               "ward_ready": bool(self.view is not None and self.view.hud.items_ready.get(6)
                                  and any(int(i.get("slot", -1)) == 6 and "ward" in str(i.get("displayName", "")).lower()
                                          for i in (me.get("items") or []))),
               "ganking": getattr(self, "_gank", None) is not None and self.intent == "objective"}
        mm = self.mm_state
        if sc is not None and mm is not None and mm.pos is not None:
            t = self._own_tower_standing_near(mm.pos, 1700.0)
            if t is not None:
                ppu = config.VISION.px_per_unit
                ctx["own_tower_px"] = (sc.me_xy[0] + (t[0] - mm.pos[0]) * ppu, sc.me_xy[1] - (t[1] - mm.pos[1]) * ppu)
        ch = sc.champ if sc is not None else None
        if ch is not None:
            name = getattr(ch, "name", "") or self._her_name(sc)
            ctx["target_flash_down"] = self.enemy_flash_down(name, time.time()) if name else False
            ctx["target_killed_me"] = macro.killed_by(data, name) if name else 0
            her = next((p for p in data.get("allPlayers", []) if str(p.get("championName")) == name), None)
            prof = self.enemy_kn.profile(name) if name else None
            if her is not None and prof is not None and prof.stats:
                lvl = int(her.get("level") or ctx["level"])
                items = her.get("items") or []
                ctx.update(target=name, target_level=lvl,
                           target_max_hp=prof.stat("hp", lvl) + self._item_stat(items, "FlatHPPoolMod"),
                           target_armor=prof.stat("armor", lvl) + self._item_stat(items, "FlatArmorMod"),
                           target_mr=prof.stat("spellblock", lvl) + self._item_stat(items, "FlatSpellBlockMod"))
            else:
                ctx.update(target=name, target_level=ctx["level"], target_max_hp=640.0 + 100.0 * (ctx["level"] - 1),
                           target_armor=30.0 + 4.5 * ctx["level"], target_mr=32.0 + 1.5 * ctx["level"])
        return ctx

    def _lane_aggression(self, d, data: dict, sc, now: float) -> float:
        """How much the lane planner values hitting her: Jev's aggression, our form, the jungler threat.
        With a floor of 0.8 while I am healthy (60%+), not behind her in HP or in levels, and their
        jungler not able to be on me: g38's strategy head gave 0.1-0.2 most of the lane (g35, the win,
        1.2-1.5), a Q on Sett was worth a tenth of a step to a better spot, and Yasuo never traded."""
        jev = (d.aggression if d is not None else 1.0) * macro.form(data)
        threat = self._jungler_threat(now or time.time())
        a = jev * {"far": 1.15, "near": 0.85}.get(threat, 1.0)
        mi = self.micro
        ch = sc.champ if sc is not None else None
        st = self.state or {}
        me_lvl = int((st.get("me") or {}).get("level") or 1)
        her_lvl = int((st.get("lane_opponent") or {}).get("level") or me_lvl)
        if (mi is not None and mi.hp_pct >= 60 and threat != "near" and me_lvl >= her_lvl - 1
                and (ch is None or ch.unit.hp * 100 <= mi.hp_pct + 10)):
            a = max(a, 0.8)
        return a

    def _lane_info(self, view, stats: dict, sc=None, now: float = 0.0) -> dict:
        """What the lane planner needs beyond the screen: who she is, her reach with the spells she
        has up, her HP pool, their tower on screen, Jev's aggression."""
        st = self.state or {}
        opp = st.get("lane_opponent") or {}
        data = self.data or {}
        me = find_me(data) or {}
        self._enemy_names = [str(p.get("championName")) for p in data.get("allPlayers", [])
                             if p.get("team") and p.get("team") != me.get("team") and p.get("championName")]
        name = self._her_name(sc) if sc is not None else str(opp.get("champion") or "")
        her = next((p for p in data.get("allPlayers", []) if str(p.get("championName")) == name), None)
        lvl = int((her or {}).get("level") or opp.get("level") or (st.get("me") or {}).get("level") or 1)
        cat = self.shop_brain.catalog if self.shop_brain is not None else None
        rng = cat.attack_range(name) if cat is not None else None
        if sc is not None and sc.champ is not None:
            # A burst on me from her: which spell, and how long it is down (enemies.py).
            drop = -(self._trail_change(0.4, now) or 0.0)
            used = self.enemy_kn.burst(now, name, sc.champ_dist or 9e9, drop, lvl)
            if used:
                self.log_lines.append(self.enemy_kn.events[-1])
        reach = self.enemy_kn.reach_now(name, now)
        down = self.enemy_kn.spells_down(name, now)
        d = self.decision
        sit = getattr(self, "situation", None)
        mia_px = None
        if sit is not None and sit.unseen >= 4 and not sit.power_play and self.mech is not None:
            # how far up the lane (along the lane, screen px) I may stand: a little past the middle, with four unseen
            mia_px = (self.lane.center + self.lane.frac(350) - self.mech.nav.progress) * self.lane.L * config.VISION.px_per_unit
        info = {"opp": name, "opp_range": rng if rng is not None else 550.0, "opp_hp": 640.0 + 95.0 * (lvl - 1),
                "mia_limit_px": mia_px,
                "opp_reach": reach if reach is not None else (rng if rng is not None else 550.0) + 150.0,
                "her_spells_down": down,
                # Yasuo's Flow: a full bar is a shield on the next champion hit (trade with it up). Only
                # when the API fills it (resourceMax > 0); unknown otherwise.
                "shield_ready": (float(stats.get("resourceMax") or 0) > 0
                                 and float(stats.get("resourceValue") or 0) >= float(stats.get("resourceMax") or 0) - 1),
                "aggression": self._lane_aggression(d, data, sc, now),
                "tower_farm_ok": bool(getattr(self, "_tower_farm_ok", False))}
        t, mm = getattr(self, "_enemy_tower_map", None), self.mm_state
        if t is not None and mm is not None and mm.pos is not None and view.me is not None:
            ppu = config.VISION.px_per_unit
            info["tower_px"] = (view.me.x + (t[0] - mm.pos[0]) * ppu, view.me.y - (t[1] - mm.pos[1]) * ppu)
        return info

    # Melee champions with a line skillshot or hook worth sidestepping.
    LINE_MELEE = {"blitzcrank", "leesin", "lee sin", "pyke", "leona", "olaf", "yasuo", "yone", "nautilus",
                  "mordekaiser", "aatrox", "rakan", "jarvaniv", "jarvan iv", "sion", "gragas", "sett",
                  "dr. mundo", "drmundo", "urgot", "illaoi", "renekton", "camille", "kled", "pantheon", "yone"}   # (Mundo's cleaver: 6-17% a throw, g45)

    def _opponent_throws_lines(self) -> bool:
        """Sidestepping only pays against line skillshots: against Nasus (melee, point-and-click) the
        juke every 0.2 s was 33 of g29's moves and the cursor it left behind aimed the next Q.
        Ranged lane opponents and the melee ones in LINE_MELEE; unknown counts as yes."""
        opp = str((self.state.get("lane_opponent") or {}).get("champion") or "")
        cat = self.shop_brain.catalog if self.shop_brain is not None else None
        rng = cat.attack_range(opp) if (cat is not None and opp) else None
        if rng is None:
            return True
        return rng >= 300 or opp.lower() in self.LINE_MELEE

    def _shove_first(self, data: dict, ap: dict, stats: dict, now: float, hp_pct: float) -> bool:
        """Before a recall that is not urgent: push the wave on screen for up to 10 s (their minions
        die, ours walk on to their tower) so the wave does not crash under our tower while we are
        gone. Not below 55% HP, with their champion on screen, or with three of them unseen."""
        v = self.view
        if v is None or hp_pct < 55 or self.jungle_state is not None or self.kit.support:
            return False
        if now - getattr(self, "_shove_t0", -1e9) > 40.0:
            self._shove_t0 = now           # a new recall decision
        if now - self._shove_t0 > 10.0:
            return False
        sit = getattr(self, "situation", None)
        if v.enemies("champion") or len(v.enemies("minion")) < 2 or not v.allies("minion") or (sit is not None and sit.unseen >= 4):
            return False
        self._shove_until = now + 0.5
        if self._micro_step(data, ap, stats, now):
            self.mech.last_action = "recall: shove the wave first"
            return True
        return False

    def _farm_the_waves(self, now: float) -> bool:
        """After laning: farm wherever their minions are, not up and down my own lane. g31 spent 72% of
        its farming after 15:00 with no minion on screen ("attack-move to lane N%" along an empty top
        lane) and made 60 CS in 37 minutes. With nothing on screen for 3 s, walk to the nearest group of
        their minions on the minimap (any lane), unless two of them stand within 1500 of it."""
        v, mm = self.view, self.mm_state
        gt = float(((self.data or {}).get("gameData") or {}).get("gameTime", 0.0))
        if (self.jungle_state is not None or self.kit.support or gt < 840 or mm is None or mm.pos is None
                or v is None or now - v.ts > 0.5):
            return False
        if v.enemies("minion"):
            self._no_wave_since = None
            return False
        if getattr(self, "_no_wave_since", None) is None:
            self._no_wave_since = now
        if now - self._no_wave_since < 3.0 or not mm.enemy_minions:
            return False
        groups = []
        sit = getattr(self, "situation", None)
        unseen_many = sit is not None and sit.unseen >= 3 and not sit.power_play
        for e in mm.enemy_minions:
            n = sum(1 for o in mm.enemy_minions if dist(o, e) < 900)
            danger = sum(1 for c in mm.enemy_champions if dist(c, e) < 1500)
            if n >= 2 and danger < 2:
                if unseen_many:
                    # Three or more of them unseen: only waves in our half. g39's Yasuo farmed deep in their
                    # half of bot at 19:00 with four unseen, then Amumu and Lissandra came out of the fog.
                    ln = min((Lane(k, self.side) for k in ("top", "mid", "bot")), key=lambda l: l.project(e)[1])
                    if ln.project(e)[0] > ln.center + ln.frac(150):
                        continue
                groups.append((dist(mm.pos, e) - 400 * n, e))
        if not groups:
            return False
        _, target = min(groups)
        # The micro keeps only red bars near its own lane (jungle monsters look like minions): farming
        # another lane's wave means playing that lane.
        near_lane = min(("top", "mid", "bot"), key=lambda n: Lane(n, self.side).project(target)[1])
        if (near_lane != self.lane.name and Lane(near_lane, self.side).project(target)[1] < 1500
                and self._lane_switch_ok(now)):
            self._switch_lane(near_lane)
        self.mech.go_map(target, now, attack=True, every=1.5)
        self.mech.last_action = f"farm: to their wave at {int(target[0])},{int(target[1])}"
        return True

    def _recall_threat(self, now: float) -> bool:
        """An enemy champion within 1300 units or an enemy minion within 700 on screen: a channel
        there is hit and cancelled. Behind the lane centre by the minimap, Yasuo channelled in the
        middle of both waves with Nasus next to him (g29, 5:31)."""
        v = self.view
        if v is None or v.me is None or now - v.ts > 0.5:
            return False
        ppu = config.VISION.px_per_unit
        d = lambda u: math.hypot(u.x - v.me.x, u.y - v.me.y) / ppu
        return (any(d(u) < 1300 for u in v.enemies("champion"))
                or any(d(u) < 700 for u in v.enemies("minion")))

    def _kit_trade_window(self, sc, mi, now: float) -> str | None:
        """The kit's own trade opener (EQ or the tornado in reach, me at least as healthy and as high
        level, her wave not around her), or None; not while clearing a camp."""
        if sc.champ is None or (self.jungle_state is not None and getattr(self, "_at_camp", False)):
            return None
        me_lvl = int((self.state.get("me") or {}).get("level") or 1)
        opp_lvl = int((self.state.get("lane_opponent") or {}).get("level") or me_lvl)
        return self.kit.trade_window(mi, sc, now, me_lvl - opp_lvl)

    def _levels_behind(self) -> float:
        """Their team's average level minus mine (0 when unknown)."""
        data = self.data or {}
        me = find_me(data) or {}
        mine = float((data.get("activePlayer") or {}).get("level") or me.get("level") or 0)
        theirs = [float(p.get("level") or 0) for p in data.get("allPlayers", []) if p.get("team") and p.get("team") != me.get("team")]
        return (sum(theirs) / len(theirs) - mine) if theirs and mine else 0.0

    def _own_tower_standing_near(self, pos, radius: float):
        """Our nearest standing lane tower within `radius` of a map point, or None."""
        own = config.BLUE_TOWERS if self.side == "ORDER" else config.RED_TOWERS
        tag = "T1" if self.side == "ORDER" else "T2"
        dead = getattr(self, "_dead_turrets", set())
        from jev.lanes import LANE_TOWER_BASE
        best = None
        for lane, base in LANE_TOWER_BASE.items():
            for k, num in enumerate((5, 4, 3)):
                i = base + k
                if i >= len(own) or f"Turret_{tag}_{LANE_LETTER[lane]}_{num:02d}_A" in dead:
                    continue
                d = dist(pos, own[i])
                if d < radius and (best is None or d < best[0]):
                    best = (d, own[i])
        return best[1] if best else None

    def _own_tower_near(self, ch, sc):
        """Our standing tower whose range covers the enemy champion, or None. Her map position is
        ours plus her screen offset; destroyed towers come from the TurretKilled events."""
        mm = self.mm_state
        if mm is None or mm.pos is None:
            return None
        ppu = config.VISION.px_per_unit
        cx = mm.pos[0] + (ch.unit.x - sc.me_xy[0]) / ppu
        cy = mm.pos[1] - (ch.unit.y - sc.me_xy[1]) / ppu
        own = config.BLUE_TOWERS if self.side == "ORDER" else config.RED_TOWERS
        tag = "T1" if self.side == "ORDER" else "T2"
        dead = getattr(self, "_dead_turrets", set())
        from jev.lanes import LANE_TOWER_BASE
        for lane, base in LANE_TOWER_BASE.items():
            for k, num in enumerate((5, 4, 3)):
                i = base + k
                if i >= len(own) or f"Turret_{tag}_{LANE_LETTER[lane]}_{num:02d}_A" in dead:
                    continue
                if dist((cx, cy), own[i]) < 775 and dist(mm.pos, own[i]) < 1000:
                    return own[i]
        return None

    def _execute(self, kit, mi, sc, now: float) -> bool:
        """The kill-secure reflex: an enemy champion whose half-second median HP is under 12% (no
        reading above 25%: a flickering bar is not a kill) within 1100 units gets the one order that
        finishes it, in any mode, retreating included."""
        best = None
        for t in self.champ_tracker.tracks.values():
            if now - t.seen > 0.25:
                continue
            rec = [h for ts, h in t.hist if now - ts <= 0.5]
            if len(rec) < 3 or max(rec) > 0.25 or sorted(rec)[len(rec) // 2] >= 0.12:
                continue
            if sc.dist(t) <= 1100 and (best is None or t.unit.hp < best.unit.hp):
                best = t
        if best is None or not self._champ_on_minimap(now):
            return False
        if kit.execute(mi, sc, best, now):
            if now - getattr(self, "_exec_logged", 0.0) > 2.0:
                self._exec_logged = now
                self.log_lines.append(f"fight: execute ({best.unit.hp * 100:.0f}% at {sc.dist(best):.0f}u, me {mi.hp_pct:.0f}%): {mi.last_action}")
            return True
        return False

    def _commit(self, ch, mi, now: float, why: str) -> None:
        """All in on this champion, and keep it the target for four seconds (the scene's default
        target is the nearest champion, which changes as a fight moves)."""
        if (mi.mode != "all_in" and mi.last_action.startswith("all in: out of reach")
                and now - mi.last_action_t < 4.0 and ch.unit.hp >= 0.25):
            return  # the chase was just given up: not straight back into it (she is still out of reach)
        if mi.mode != "all_in":
            self.log_lines.append(f"fight: {why}")
        mi.set_mode("all_in", now)
        self._focus_id, self._focus_until = ch.id, now + 4.0

    def _focus_target(self, sc, mi, now: float) -> None:
        """The champion to fight: the one we committed to while it is in view; otherwise, with two
        or more in reach, the weakest by a clear margin (a kill), not simply the nearest."""
        if sc.champ is None:
            return
        live = [t for t in self.champ_tracker.tracks.values() if now - t.seen < 0.25]
        pick = None
        if now < getattr(self, "_focus_until", 0.0):
            pick = next((t for t in live if t.id == self._focus_id), None)
        if pick is None:
            reach = [t for t in live if sc.dist(t) < 800]
            if len(reach) >= 2:
                weakest = min(reach, key=lambda t: t.unit.hp)
                if weakest.unit.hp < sc.champ.unit.hp - 0.15:
                    pick = weakest
        if pick is not None and pick is not sc.champ:
            sc.champ, sc.champ_dist = pick, sc.dist(pick)

    def _recent_gold_jump(self, seconds: float) -> float:
        """The largest single gold gain in the last `seconds` (wall time): camps, kills and last hits
        pay in one step, passive income in small ones."""
        gh = getattr(self, "_lh_gold", None)
        if not gh:
            return 0.0
        now = time.time()
        pts = [g for t, g in gh if now - t <= seconds]
        return max((b - a for a, b in zip(pts, pts[1:])), default=0.0)

    @staticmethod
    def _took(sc, why: str, ret):
        """Mark killable minions with what took this frame instead of the farm step (the last-hit
        audit reports it), and pass the return value through."""
        for t in sc.killable_auto:
            if getattr(t, "block", "") == "farm step not reached":
                t.block = why
        return ret

    def _audit_lasthits(self, mi, now: float) -> None:
        """Every enemy minion that vanishes while low (under 35%) and within 800 units: did we try it?
        If not, was it ever killable by our numbers, and how far was it? Logged each minute with the
        paid rates, so farming is fixed from counts (g19: 48 tries for ~90 minions in 8 minutes)."""
        if mi is None:
            return
        audit = self._lh_audit = getattr(self, "_lh_audit", collections.Counter())
        for tr in self.min_tracker.dropped:
            if tr.unit.team != "enemy" or tr.unit.kind != "minion" or tr.unit.hp > 0.35:
                continue
            d = getattr(tr, "last_dist", 9e9)
            if d > 800:
                continue
            if now - mi.attacked_ids.get(tr.id, 0.0) < 3.0:
                audit["tried"] += 1
            elif getattr(tr, "was_killable", False):
                audit[f"killable, not taken ({getattr(tr, 'block', '?')}; {self.micro.mode if self.micro else '?'})"] += 1
            else:
                # (with the mode: 300-800 units away while backing off is the stand spot's caution;
                # while farming it is the spot itself)
                audit[f"never killable {'<300' if d < 300 else '300-600' if d < 600 else '600-800'}u"
                      + ("" if d < 300 else f" ({self.micro.mode if self.micro else '?'})")] += 1
        if mi.attacked_ids and len(mi.attacked_ids) > 200:
            mi.attacked_ids = {k: v for k, v in mi.attacked_ids.items() if now - v < 10}

    def _lasthit_check(self, gold: float, now: float) -> None:
        """Did each last-hit attempt pay? A CS is a gold jump of 12+ above passive income within
        a second of the order. Tallied by kind and minion HP bucket; logged once a minute, so
        thresholds can be tuned from numbers instead of guesses."""
        mi = self.micro
        if mi is None:
            return
        gh = self._lh_gold = getattr(self, "_lh_gold", collections.deque(maxlen=400))
        gh.append((now, gold))
        stats = self._lh_stats = getattr(self, "_lh_stats", collections.defaultdict(lambda: [0, 0]))
        keep = []
        learner = getattr(mi, "learner", None)
        for entry in mi.lh_pending:
            t, kind, hp = entry[:3]
            z = entry[3] if len(entry) > 3 else None  # the planner's predicted margin, for the learner
            win = 2.0 if kind.startswith("auto") else 1.1  # an auto may walk up to ~1 s first (reach: auto range + 380)
            if now - t < win:
                keep.append(entry)
                continue
            before = [g for tt, g in gh if tt <= t]
            after = [g for tt, g in gh if t < tt <= t + win]
            # A caster minion is worth 14 gold early: the old bar (12 + passive = 14.3) never counted them.
            ok = bool(before and after) and max(b - a for a, b in zip([before[-1]] + after, after)) >= 11
            if learner is not None:
                learner.lasthit(kind, z, ok)
            key = f"{kind} {int(hp * 100) // 5 * 5}%"
            stats[key][0] += 1
            stats[key][1] += int(ok)
            stats[kind][0] += 1
            stats[kind][1] += int(ok)
            base = kind.split("-")[0]
            if base != kind:  # "Q" and "auto" totals next to the per-role ones ("Q-melee", "auto-caster")
                stats[base][0] += 1
                stats[base][1] += int(ok)
        mi.lh_pending = keep
        if now - getattr(self, "_lh_logged", 0.0) > 60 and stats:
            self._lh_logged = now
            parts = [f"{k} {v[1]}/{v[0]}" for k, v in sorted(stats.items())]
            self.log_lines.append("lasthits paid: " + ", ".join(parts))
            if learner is not None:
                self.log_lines.append(learner.summary())
                self.log_lines.append(HP_MODEL.summary())
                learner.save()
            if mi.skill_stats:
                self.log_lines.append("skillshots on champions (landed/thrown): "
                                      + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(mi.skill_stats.items())))
            if mi.q_stats:
                # Units hit per Q (the line, the tornado, the E+Q circle): the more per cast, the better.
                self.log_lines.append("Q hits: " + " | ".join(
                    f"{k} {v['casts']} casts, {v['units'] / max(1, v['casts']):.1f} units each, 2+ {v['multi']}, on a champion {v['champ']}"
                    for k, v in sorted(mi.q_stats.items())))
            audit = getattr(self, "_lh_audit", None)
            if audit:
                self.log_lines.append("lasthit audit (low minions that died near me): "
                                      + ", ".join(f"{k} {v}" for k, v in audit.most_common()))

    # -- the fight head -----------------------------------------------------------------------
    def _enemies_near_on_map(self, radius: float) -> list[tuple[float, float]]:
        mm = self.mm_state
        if mm is None or mm.pos is None:
            return []
        return [e for e in mm.enemy_champions if dist(mm.pos, e) <= radius]

    def _read_names(self, frame, v, ts: float) -> None:
        """The name above an enemy champion's bar (the champion, for bots), once per new champion
        on screen and at most every 0.3 s (~25 ms each). The actor gives it to the nearest track."""
        if not self._enemy_names or not namereader.available() or ts - self._name_tried < 0.3:
            return
        try:
            self._read_names_now(frame, v, ts)
        except Exception:  # noqa: BLE001  (tracks change under us in the actor thread): a name later
            pass

    def _read_names_now(self, frame, v, ts: float) -> None:
        for u in v.enemies("champion"):
            named = any(getattr(t, "name", "") and math.hypot(t.unit.x - u.x, t.unit.y - u.y) < 80
                        for t in list(self.champ_tracker.tracks.values()))
            if named or any(ts - t0 < 1.0 and math.hypot(x - u.x, y - u.y) < 80 for t0, x, y, _ in self._name_marks):
                continue
            self._name_tried = ts
            try:
                name = namereader.identify(frame, u.bar, self._enemy_names)
            except Exception:  # noqa: BLE001  a reader failure only costs the name
                name = None
            if name:
                self._name_marks.append((ts, u.x, u.y, name))
            return

    # Champions whose own spells blink (no Flash needed to jump 300-480 units in an instant).
    BLINKERS = {"ezreal", "kassadin", "katarina", "leblanc", "shaco", "zed", "vayne", "fiora", "talon", "pyke", "qiyana"}

    def _note_blinks(self, champs: list, now: float) -> None:
        """An enemy that jumped 300-480 units in an instant (micro.UnitTracker._blinked) and has Flash
        used it: down for ~5 minutes (the guides: a lane whose laner burned Flash is gankable for five
        minutes; a kill without Flash to escape is a kill)."""
        fd = self.flash_down = getattr(self, "flash_down", {})
        for tr in champs:
            if not tr.blinks or tr.blinks[-1][0] != now:
                continue
            name = getattr(tr, "name", "") or ""
            key = name.lower().replace(" ", "").replace("'", "").replace(".", "")
            if key in self.BLINKERS:
                self.log_lines.append(f"enemy blink: {name} jumped {tr.blinks[-1][1]:.0f}u (a spell, not Flash)")
                continue
            p = next((p for p in (self.data or {}).get("allPlayers", []) if str(p.get("championName")) == name), None)
            if p is not None and "flash" not in " ".join(str((v or {}).get("displayName", "")) for v in (p.get("summonerSpells") or {}).values()).lower():
                continue   # no Flash to use
            fd[name or f"track {tr.id}"] = now + 300.0
            self.log_lines.append(f"enemy Flash: {name or 'a champion'} jumped {tr.blinks[-1][1]:.0f}u, Flash down ~5 min")

    def enemy_flash_down(self, name: str, now: float) -> bool:
        return now < getattr(self, "flash_down", {}).get(name, 0.0)

    def _name_tracks(self, champs: list, now: float) -> None:
        gt = float(((self.data or {}).get("gameData") or {}).get("gameTime", 0.0))
        for tr in champs:
            if getattr(tr, "name", ""):
                if self.phase == "lane" and gt < 840 and now - getattr(tr, "_counted", 0.0) >= 1.0:
                    tr._counted = now
                    self._seen_names[tr.name] += 1     # seconds on screen in lane, first 14 minutes
                continue
            for t0, x, y, name in reversed(self._name_marks):
                if now - t0 < 1.5 and math.hypot(tr.unit.x - x, tr.unit.y - y) < 90:
                    tr.name = name
                    last = getattr(self, "_name_logged", ("", 0.0))
                    if name != last[0] or now - last[1] > 30.0:
                        self._name_logged = (name, now)
                        self.log_lines.append(f"on screen: {name}")
                    break

    def _opp_hint(self) -> str | None:
        """The enemy champion seen longest on screen while laning (for when positions are empty)."""
        if self.jungle_state is not None or not self._seen_names:
            return None
        name, secs = self._seen_names.most_common(1)[0]
        return name if secs >= 10 else None

    def _her_name(self, sc) -> str:
        """The champion in front of me: her bar label when read, else the lane opponent."""
        if sc.champ is not None and getattr(sc.champ, "name", ""):
            return sc.champ.name
        return str((self.state.get("lane_opponent") or {}).get("champion") or "")

    def _trail_change(self, seconds: float, now: float) -> float | None:
        old = [h for t, h in self._self_trail if now - t >= seconds - 0.05]
        return (self._self_trail[-1][1] - old[-1]) if old and self._self_trail else None

    def _fight_features(self, sc, champs, now: float, data: dict) -> dict:
        """What the fight head sees: both sides' HP and its movement, what is ready, levels, items,
        minions, towers, and who else is close on the minimap."""
        ppu = config.VISION.px_per_unit
        ap = data.get("activePlayer", {})
        cs = ap.get("championStats", {})
        me_p = find_me(data) or {}
        st = self.state or {}
        mi = self.micro
        rnd = lambda v, k=25: None if v is None else int(round(v / k) * k)
        pct = lambda v: None if v is None else round(v * 100)
        mx, my = sc.me_xy
        enemies = {}
        for tr in sorted(champs, key=sc.dist)[:3]:
            d = sc.dist(tr)
            vx, vy = tr.velocity(now)
            dx, dy = mx - tr.unit.x, my - tr.unit.y
            n = math.hypot(dx, dy) or 1.0
            approach = (vx * dx + vy * dy) / n / ppu  # units/s toward me
            around = sum(1 for t in sc.minions if math.hypot(t.unit.x - tr.unit.x, t.unit.y - tr.unit.y) <= 500 * ppu)
            info = {
                "hp_percent": pct(tr.unit.hp), "hp_last_1s": pct(tr.hp_change(now, 1.0)), "hp_last_3s": pct(tr.hp_change(now, 3.0)),
                "distance": rnd(d), "direction": actions.compass(tr.unit.x - mx, tr.unit.y - my),
                "moving": "toward me" if approach > 90 else ("away from me" if approach < -90 else "holding"),
                "enemy_minions_around_them": around,
            }
            name = getattr(tr, "name", "")
            if name:
                down = self.enemy_kn.spells_down(name, now)
                prof = self.enemy_kn.profile(name)
                info["champion"] = name
                if prof is not None:
                    info["threat_reach"] = rnd(prof.reach(self.enemy_kn.up(name, now)))
                info["her_spells_on_cooldown"] = down
                if self.enemy_flash_down(name, now):
                    info["flash"] = "used: down for minutes, she cannot escape with it"
            info["describe"] = (f"{name or 'enemy champion'} at {info['hp_percent']}% HP, {info['distance']} units {info['direction']}, "
                                f"{info['moving']}" + (f", {'/'.join(info['her_spells_on_cooldown'])} on cooldown" if info.get("her_spells_on_cooldown") else "")
                                + (", Flash down" if info.get("flash") else ""))
            enemies[f"enemy_champion_{tr.id}"] = info
        my_around = sum(1 for t in sc.minions if sc.dist(t) <= 500)
        summ = {n: ("ready" if sc.ready.get(k) else "not ready") for n, k in zip(mi.summoners, "DF") if n}
        me = {
            "champion": self.kit.name, "level": ap.get("level"), "hp_percent": round(mi.hp_pct),
            "hp": f"{int(cs.get('currentHealth', 0))}/{int(cs.get('maxHealth', 0))}",
            "hp_last_1s": None if self._trail_change(1.0, now) is None else round(self._trail_change(1.0, now)),
            "hp_last_3s": None if self._trail_change(3.0, now) is None else round(self._trail_change(3.0, now)),
            "abilities": self.kit.me_state(sc, mi, now), "summoner_spells": summ,
            "attack_damage": round(float(cs.get("attackDamage", 0))), "armor": round(float(cs.get("armor", 0))),
            "magic_resist": round(float(cs.get("magicResist", 0))),
            "items": [i.get("displayName") for i in me_p.get("items", []) if i.get("displayName")],
            "enemy_minions_around_me": my_around, "ally_minions_on_screen": sc.allies,
            "allied_champions_on_screen": len(sc.ally_champs),
        }
        if float(cs.get("resourceMax") or 0) > 0 and str(cs.get("resourceType", "")).upper() in ("WIND", "FLOW"):
            full = float(cs.get("resourceValue") or 0) >= float(cs.get("resourceMax") or 0) - 1
            me["passive_shield"] = "ready (Flow full: the next champion hit on me is shielded)" if full else "charging"
        mm = self.mm_state
        where: dict[str, Any] = {"inside_enemy_tower_range": bool(getattr(self, "_near_enemy_tower", False))}
        mapinfo: dict[str, Any] = {}
        if mm is not None and mm.pos is not None:
            own = config.BLUE_TOWERS if self.side == "ORDER" else config.RED_TOWERS
            theirs = config.RED_TOWERS if self.side == "ORDER" else config.BLUE_TOWERS
            where["distance_to_my_nearest_tower"] = rnd(min(dist(mm.pos, t) for t in own), 100)
            where["distance_to_their_nearest_tower"] = rnd(min(dist(mm.pos, t) for t in theirs), 100)
            near = lambda pts: [{"distance": rnd(dist(mm.pos, e), 100), "direction": actions.compass(e[0] - mm.pos[0], mm.pos[1] - e[1])}
                                for e in sorted(pts, key=lambda e: dist(mm.pos, e)) if dist(mm.pos, e) <= 4000]
            mapinfo = {"enemy_champions_within_4000": near(mm.enemy_champions),
                       "allied_champions_within_4000": near(mm.ally_champions),
                       "enemy_champions_not_on_the_minimap": max(0, 5 - len(mm.enemy_champions))}
        # Derived numbers: Jev read a two-on-one at half HP, losing 14% a second, as danger 0.27 when it
        # had to infer these from the raw fields.
        loss1 = me["hp_last_1s"]
        on_map_close = lambda pts: sum(1 for e in pts if mm is not None and mm.pos is not None and dist(mm.pos, e) <= 1000)
        them = max(sum(1 for v in enemies.values() if (v["distance"] or 9e9) <= 1000), on_map_close(mm.enemy_champions) if mm else 0)
        us = 1 + max(len(sc.ally_champs), on_map_close(mm.ally_champions) if mm else 0)
        numbers = {
            "enemy_champions_within_1000": them, "our_champions_within_1000_including_me": us,
            "seconds_i_last_at_this_rate": (round(mi.hp_pct / -loss1, 1) if loss1 is not None and loss1 < -1 else None),
            "their_hp_total_percent_on_screen": sum(v["hp_percent"] or 0 for v in enemies.values()),
        }
        return {"numbers": numbers, "me": me, "enemies_on_screen": enemies, "where": where, "map": mapinfo,
                "lane_opponent": st.get("lane_opponent"), "enemy_team": st.get("enemy_lineup"),
                "game_time": (st.get("game") or {}).get("time")}

    def _apply_fight_read(self, fr: FightRead, sc, mi, now: float) -> None:
        """Jev's plan becomes the combo layer's mode. Code keeps three floors: no fight two on one
        without an ally unless Jev is sure, no tower dive unless the target is nearly dead and Jev
        is sure, and an escape when Jev says I am about to die."""
        if fr.focus and sc.champ is not None:
            tid = fr.focus.rsplit("_", 1)[-1]
            pick = next((t for t in self.champ_tracker.tracks.values() if str(t.id) == tid), None)
            if pick is not None:
                sc.champ, sc.champ_dist = pick, sc.dist(pick)
        ch = sc.champ
        plan = fr.plan
        # Hysteresis: a new plan takes over only when Jev is sure of it (p >= 0.5) or reads it twice in a
        # row. At p 0.3-0.4 the plan flipped between back_off, poke and farm every half second and Yasuo
        # never traded Nasus once (g29). Danger and escapes are never held back.
        held = getattr(self, "_fight_plan_held", None)
        if held is not None and plan != held and fr.plan_probs.get(plan, 0.0) < 0.5 and fr.in_danger < 0.75 and plan != "escape":
            if getattr(self, "_fight_plan_pending", None) != plan:
                self._fight_plan_pending = plan
                plan = held
            else:
                self._fight_plan_pending = None
        if plan != "escape":
            self._fight_plan_held = plan
        if fr.in_danger >= 0.75 and plan not in ("escape", "back_off"):
            plan = "escape" if fr.in_danger >= 0.85 else "back_off"
        if plan in ("all_in", "trade") and ch is None:
            plan = "farm"
        their_hp = max((v.get("hp_percent") or 0) for v in (fr.state.get("enemies_on_screen") or {}).values()) if fr.state.get("enemies_on_screen") else 0
        if (plan == "back_off" and fr.in_danger < 0.35 and mi.hp_pct >= 70 and mi.hp_pct >= their_hp + 10
                and sc.enemy_champs <= 1):
            # (Only when clearly ahead: at 60% vs 57% the override kept Yasuo in a burst that took 57%
            # in three seconds, g11; Jev's own back_off had been right.)
            # Jev backed off 75% of the time at a median 90% HP against one champion (g09) while
            # its own danger read said 0.2-0.3; poke (skillshots from range, else farm) had the best
            # measured trades (-3.7% mine vs -16% theirs per 3 s). Its danger score decides.
            plan = "poke"
        if plan == "all_in" and fr.win_all_in < 0.55:
            plan = "trade" if fr.trade_worth >= 0.55 else "farm"
        if plan == "trade" and (fr.trade_worth < 0.55 or now < getattr(mi, "trade_cooldown_until", 0.0)):
            # Jev's trade_worth sits near 0.5 most of the lane, and a trade that just ended was
            # restarted on the next read: Yasuo chased non-stop and had under 10 CS at 4:00 (g09).
            plan = "poke"
        outnumbered = sc.enemy_champs >= 2 and not sc.ally_champs
        if plan in ("all_in", "trade") and outnumbered and fr.win_all_in < 0.8:
            plan = "back_off"
        if plan in ("all_in", "trade") and self._her_team_near(sc) and fr.win_all_in < 0.8:
            plan = "poke"   # her team by her on the minimap: not after her (g39, 37:03 and 42:47)
        if (plan in ("all_in", "trade") and ch is not None and mi.hp_pct < 25 and ch.unit.hp > mi.hp_pct / 100 + 0.15
                and fr.win_all_in < 0.8):
            plan = "back_off"  # losing floor: low and behind
        if plan in ("all_in", "trade") and ch is not None and not sc.ally_champs and ch.unit.hp > 0.3:
            # Far behind in HP against a healthy one: no trade. g31: Q-trading Mordekaiser at 46% against his
            # 92% on Jev's trade_worth 0.70, then dead fleeing at 13:00. Tighter once this matchup's
            # exchanges have been losing (the learner's trade edge).
            lr = getattr(mi, "learner", None)
            edge = lr.edge.get(lr.opponent) if lr is not None else None
            gap = ch.unit.hp * 100 - mi.hp_pct
            if gap > (5 if (edge is not None and edge < 0) else 20):
                plan = "back_off" if mi.hp_pct < 50 else "poke"
        if plan in ("all_in", "trade") and getattr(self, "_near_enemy_tower", False) and not (
                ch is not None and ch.unit.hp < 0.25 and fr.win_all_in >= 0.8):
            plan = "poke"
        if (plan in ("poke", "farm") and ch is not None and fr.in_danger < 0.5 and not outnumbered
                and not getattr(self, "_near_enemy_tower", False) and now >= getattr(mi, "trade_cooldown_until", 0.0)
                and mi.mode not in ("trade", "all_in")):
            # A fresh Jev read skipped the kit's trade opener altogether, and Jev's trade_worth sat under
            # its 0.55 floor 107 of 165 times it wanted a trade: no trade on Nasus in seven minutes (g29).
            # With Jev's danger low, the kit's concrete window (EQ or the tornado in reach, me at least as
            # healthy, her wave not around her) opens it.
            window = self._kit_trade_window(sc, mi, now)
            if window == "all_in" and fr.win_all_in < 0.4:
                window = "trade"
            if window:
                plan = window
                self.log_lines.append(f"fight: trade window under Jev's {fr.plan}, {window} "
                                      f"({ch.unit.hp * 100:.0f}% at {sc.champ_dist or 0:.0f}u, me {mi.hp_pct:.0f}%)")
        mode = {"all_in": "all_in", "trade": "trade", "poke": "poke", "farm": "farm", "back_off": "back_off", "escape": "back_off"}[plan]
        if mi.mode != mode and not (mode == "trade" and mi.mode == "trade"):
            if not (mi.mode == "trade" and mode in ("poke", "farm") and now - mi.mode_since < 2.0):  # let a started trade finish
                mi.set_mode(mode, now)
        if plan != getattr(self, "_last_fight_plan", None):
            self._last_fight_plan = plan
            self.log_lines.append(f"fight(jev): {plan} <- {fr.summary()}")
        if plan in ("back_off", "escape"):
            enemies = (fr.state.get("enemies_on_screen") or {}).values()
            coming = sum(1 for v in enemies if v.get("moving") == "toward me")
            close = int((fr.state.get("numbers") or {}).get("enemy_champions_within_1000") or 0)
            # Jev's gank read sits near 0.6-0.7 whenever enemies are off the minimap: alone it retreated
            # Yasuo from lane every few seconds (g13). Danger >= 0.5 and two enemies close cover the real ones.
            gank = fr.gank_coming >= 0.75 and (fr.in_danger >= 0.4 or mi.hp_pct < 50)
            if fr.in_danger >= 0.5 or close >= 2 or coming >= 2 or gank or plan == "escape":
                # A real retreat, not a step: backing off 260 units while the strategy still said farm
                # let two full-HP champions walk up and burst Yasuo from 62% (g11, 14:12).
                self.guards.retreat_until = max(self.guards.retreat_until, now + 3.0)
                if getattr(self, "_retreat_logged", 0.0) < now - 3.0:
                    self._retreat_logged = now
                    self.log_lines.append(f"fight(jev): retreat ({close} close, {coming} coming, danger {fr.in_danger:.2f}, gank {fr.gank_coming:.2f})")
        mi.fight_owned_until = now + 0.9  # the tactical head's mode picks stand down meanwhile
        mi.flash_in_ok = plan == "all_in" and fr.win_all_in >= 0.75 and ch is not None and ch.unit.hp < 0.3
        if plan == "escape":
            self._force_escape_until = now + 1.0
        self.fights.log.record(fr, plan, self._metrics())

    def _champ_on_minimap(self, now: float, radius: float = 1800.0) -> bool:
        """Where monsters live (a jungle camp, the dragon or baron pit), a champion-sized bar must
        have an enemy champion icon within `radius` of us on the minimap to count. Elsewhere vision
        alone decides: in lane the enemy icon hides under our own icon and camera box (real
        champions at 300 units were dropped, game 4). True when the minimap is unavailable."""
        mm = self.mm_state
        if mm is None or mm.pos is None or now - mm.ts > 1.0:
            return True
        if self.jungle_state is None and self.lane.project(mm.pos)[1] < 700:
            return True  # a laner on his lane: mid passes within 1300 of the raptors (a real fight was dropped, g06)
        pits = [map_places.places(self.side)[k][0] for k in ("dragon_pit", "baron_pit")]
        monsters_near = any(dist(mm.pos, c) < 1000 for c in list(camps(self.side).values()) + pits)
        if not monsters_near:
            return True
        return any(dist(mm.pos, e) <= radius for e in mm.enemy_champions)

    def _pause_guard(self, data: dict, now: float) -> None:
        """Game time frozen for 10 s with the API alive: the game is paused (only possible in custom
        games). Log it and type /unpause once per pause."""
        gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
        if gt != getattr(self, "_gt_last", None):
            self._gt_last, self._gt_changed_at, self._unpaused = gt, now, False
            return
        if now - self._gt_changed_at > 10.0 and not self._unpaused and self.ctl.keys_ok():
            self._unpaused = True
            self.log_lines.append("game paused: typing /unpause")
            with self.ctl.slow():
                self.ctl.key("return")
                time.sleep(0.4)
                self.ctl.type_text("/unpause", per_char_ms=90)
                time.sleep(0.3)
                self.ctl.key("return")

    def _smite_reflex(self, ctx, inp, now: float) -> bool:
        """Smite the camp's big monster when it is low (large monsters first, else the healthiest
        red bar there, since the big one outlasts the small ones)."""
        sc, mi = ctx.sc, ctx.mi
        slot = next((i for i, n in enumerate(inp.summoners, 1) if n == "smite"), None)
        if slot is None or not sc.ready.get("DF"[slot - 1]) or not sc.minions:
            return False
        if now - getattr(self, "_smite_t", 0.0) < 1.5:
            return False  # one try at a time: a cast that did not land re-fired every tick (14 in 2 s, g05)
        big = [t for t in sc.minions if t.unit.kind == "monster"]
        if not big:
            return False  # Smite is for the large monster; small ones were smitten at 3-6%
        tgt = min(big, key=lambda t: t.unit.hp)
        if tgt.unit.hp > 0.25 or sc.dist(tgt) > 550:
            return False
        self._smite_t = now
        mi.ctl.cast(mi.kb.summoner(slot), *mi._pt(tgt.unit.x, tgt.unit.y), mi.kb.quick(f"evtCastAvatarSpell{slot}"))
        mi._ordered(now, "Smite")
        self.log_lines.append(f"smite at {int(tgt.unit.hp * 100)}%")
        return True

    def _jungle_step(self, data: dict, ap: dict, stats: dict, now: float) -> None:
        """Clear camps along the route, then the nearest one that is up. Fights on the way are
        answered with one-shot tactical moves; strategy sends the jungler elsewhere with go_to."""
        js, m = self.jungle_state, self.mech
        gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
        pos = self.mm_state.pos if self.mm_state is not None else None
        if js.current is None:
            js.current = js.next_camp(pos, gt)
            self.log_lines.append(f"jungle: next camp {js.current}")
        pt = camps(self.side)[js.current]
        big = js.current in BIG
        d = math.dist(pos, pt) if pos is not None else None
        if d is None or d > 900:
            if self._micro_step(data, ap, stats, now, standing=False):
                return
            m.go_map(pt, now, attack=False, every=1.0)
            m.last_action = f"jungle: to {js.current}"
            return
        if not js.up(js.current, gt):
            m.last_action = f"jungle: waiting for {js.current}"
            if now - getattr(self, "_wait_step_t", 0.0) > 15.0:
                # A minute of standing still before the camps spawn drew an AFK warning (g07).
                self._wait_step_t = now
                sgn = 1 if int(now / 15) % 2 else -1
                m.go_map((pt[0] + sgn * 250, pt[1] - 250), now, attack=False, every=0.0)
            return
        if js.arrived_at is None or js.arrived_at < FIRST_SPAWN:
            js.arrived_at = max(gt, FIRST_SPAWN)
        sc_before = len(self.min_tracker.tracks)
        self._at_camp = True
        if self.micro is not None:
            self.micro.at_camp = True
        try:
            acted = self._micro_step(data, ap, stats, now, standing=True, camp_pt=pt, camp_big=big)
        finally:
            self._at_camp = False
            if self.micro is not None:
                self.micro.at_camp = False
        # Tiny red bars (a few px) flicker around a champion carrying the red buff and read as
        # nearly dead minions: they kept a dead camp "alive" for a minute (g07). Only a large
        # monster or a unit above 12% keeps the camp going.
        real = [t for t in (self.scene.minions if self.scene is not None else []) if t.unit.kind == "monster" or t.unit.hp >= 0.12]
        if real:
            js.last_seen_monster = gt
            js.low_seen = min(js.low_seen, min(t.unit.hp for t in real))
            bigs = [t for t in real if t.unit.kind == "monster"]
            if bigs:
                js.big_last = min(t.unit.hp for t in bigs)
            return
        if self.scene is not None and self.scene.minions and gt - js.last_seen_monster <= 3.0:
            js.low_seen = min(js.low_seen, min(t.unit.hp for t in self.scene.minions))
            return
        # Cleared: monsters were seen here and none for 3 s; or none at all 20 s after arriving
        # (someone else took the camp). "None seen for 3 s" alone marked red cleared before it spawned.
        seen_here = js.last_seen_monster >= js.arrived_at
        if seen_here and gt - js.last_seen_monster > 3.0 and (js.low_seen > 0.35 or (js.big_last is not None and js.big_last > 0.3)):
            # Gone while still healthy: we walked off or it leashed (red buff back to full, g05). Retry.
            # The large monster's own last reading decides when there was one: a small wolf at 5% marked
            # g36's blue "killed" while the buff itself had healed back to 69% off screen (Lee level 2 at 5:15).
            js.arrived_at, js.last_seen_monster, js.low_seen, js.big_last = None, 0.0, 1.0, None
            return
        js_low = js.low_seen
        if seen_here and gt - js.last_seen_monster > 3.0 and gt - js.arrived_at < 8.0 and self._recent_gold_jump(6.0) < 20:
            # Too quick for a kill and no camp gold: a phantom low bar and a moment of not seeing the
            # camp marked red "cleared" 3 s after it spawned, blue 13 s after wolves; Lee skipped camps
            # and was level 4 against Warwick's 6 at 7:30 (g21). Keep at it.
            return
        if (seen_here and gt - js.last_seen_monster > 3.0) or (not seen_here and gt - js.arrived_at > 20.0):
            camp, how = js.current, ("killed" if seen_here else "empty")
            js.mark_cleared(camp, gt)
            self.log_lines.append(f"jungle: cleared {camp} at {int(gt)}s ({how}, lowest {js_low:.0%})")
            if self.logfile:
                # Kept per game so a harness restart does not walk back to camps already taken.
                try:
                    with open(f"{self.logfile}.jungle.json", "w") as fh:
                        json.dump({"cleared": js.cleared, "route_i": js.route_i}, fh)
                except OSError:
                    pass
            return
        if not acted:
            m.go_map(pt, now, attack=True, every=1.2)  # step onto the camp so it aggroes
            m.last_action = f"jungle: pulling {js.current}"

    def _ward_reflex(self, sc, inp, view, now: float) -> bool:
        """Ward the river flank of the lane: after 2:30, trinket ready, no fight, a minute since the
        last ward, alternating sides. Jev never picked the ward action in three games, and ganks came
        out of the fog (g11, g13)."""
        mi = self.micro
        gt = float(((self.data or {}).get("gameData") or {}).get("gameTime", 0.0))
        if (self.kit.support or self.jungle_state is not None or gt < 150 or mi.mode in FIGHT_MODES
                or (sc.champ is not None and (sc.champ_dist or 9e9) < 1000)
                or now - getattr(self, "_ward_t", 0.0) < 60 or not view.hud.items_ready.get(6)
                or not any(int(i.get("slot", -1)) == 6 and "ward" in str(i.get("displayName", "")).lower() for i in inp.items)):
            return False
        prog = self.mech.nav.progress if self.mech else 0.5
        if not (self.lane.own_tower + 0.03 <= prog <= self.lane.center + 0.12):
            return False
        fx, fy = self.lane.screen_dir(prog)
        side = 1 if int(now / 60) % 2 else -1
        px, py = -fy * side, fx * side  # perpendicular to the lane on screen: one river flank, then the other
        ppu = config.VISION.px_per_unit
        x, y = sc.me_xy[0] + px * 560 * ppu, sc.me_xy[1] + py * 560 * ppu
        mi.ctl.cast(self.kb.vision_item, *mi._pt(x, y), self.kb.quick("evtUseVisionItem"))
        self._ward_t = now
        mi._ordered(now, "ward the river flank")
        self.log_lines.append(f"ward: river flank ({'one' if side > 0 else 'other'} side) at lane {prog:.0%}")
        return True

    def _potion_reflex(self, inp, now: float) -> bool:
        """Drink a potion under 45% HP (every 15 s at most, not in base): Jev's potion one-shot is
        one option among twenty and was rarely picked while the HP bled out in lane and camps."""
        if inp.hp_pct >= 45 or now - self._potion_at < 15 or self._at_fountain():
            return False
        for it in inp.items:
            if any(p in str(it.get("displayName", "")).lower() for p in actions.POTIONS):
                self.micro.ctl.press(self.micro.kb.item(int(it.get("slot", 0)) + 1))
                self._potion_at = now
                self.micro._ordered(now, "potion")
                self.log_lines.append(f"potion at {inp.hp_pct:.0f}% HP")
                return True
        return False

    def _escape_reflex(self, ctx, inp, now: float) -> bool:
        """Burst incoming (a quarter of HP gone in the damage window, under 45% HP, an enemy
        champion within 700 units): Flash toward our tower, else a defensive summoner, else a
        potion. Frame-level, no Jev wait; once every 12 s."""
        sc = ctx.sc
        hp, lost = getattr(self, "_hp_now", 100.0), getattr(self, "_hp_lost", 0.0)
        forced = now < getattr(self, "_force_escape_until", 0.0) and hp < 50 and sc.champ is not None
        # Losing a quarter of the HP under 45% is reason enough; the damage source is not always the
        # champion in view (100% -> 11% in six seconds with the only visible enemy at 900 units and no
        # Flash, g13), so any champion within 1000 units, or none visible, counts.
        if not forced and not (lost >= 25 and hp < 45 and (sc.champ is None or (sc.champ_dist or 9e9) < 1000)):
            return False
        home = tuple(-v for v in (self.lane.screen_dir(self.mech.nav.progress) if self.mech else ctx.mi.fwd))
        if (forced or hp >= 25) and self.kit.escape(ctx.mi, sc, now, home):
            return True  # the kit's dash first; Flash stays for the next tick if still in trouble (and for kills)
        if now - getattr(self, "_escape_t", 0.0) < 12.0:
            return False
        self._escape_t = now
        mi = ctx.mi
        fx, fy = self.lane.screen_dir(self.mech.nav.progress) if self.mech else mi.fwd
        mx, my = sc.me_xy
        back = (mx - fx * 350 * config.VISION.px_per_unit, my - fy * 350 * config.VISION.px_per_unit)
        for slot, (name, hud) in enumerate(zip(inp.summoners, "DF"), 1):
            if name == "flash" and sc.ready.get(hud):
                mi.ctl.cast(mi.kb.summoner(slot), *mi._pt(*back), mi.kb.quick(f"evtCastAvatarSpell{slot}"))
                mi._ordered(now, "escape: Flash toward tower")
                self.log_lines.append("escape: Flash toward tower")
                return True
        for slot, (name, hud) in enumerate(zip(inp.summoners, "DF"), 1):
            if name in ("heal", "barrier", "ghost", "exhaust") and sc.ready.get(hud):
                if name == "exhaust":
                    mi.ctl.cast(mi.kb.summoner(slot), *mi._pt(sc.champ.unit.x, sc.champ.unit.y), mi.kb.quick(f"evtCastAvatarSpell{slot}"))
                else:
                    mi.ctl.press(mi.kb.summoner(slot))
                mi._ordered(now, f"escape: {name}")
                self.log_lines.append(f"escape: {name}")
                return True
        for it in inp.items:
            if "potion" in str(it.get("displayName", "")).lower() and now - self._potion_at > 12:
                mi.ctl.press(mi.kb.item(int(it.get("slot", 0)) + 1))
                self._potion_at = now
                mi._ordered(now, "escape: potion")
                return True
        return False

    def _objective_plan(self, data: dict, now: float):
        """Objectives that make sense, by rule: push the tower while the lane opponent is dead and our
        wave is there; take dragon or baron when the team is at the pit; after 15:00 join the team's
        group and fight with it. Returns (kind, map point, label) or None."""
        mm = self.mm_state
        st = self.state or {}
        me = st.get("me") or {}
        if mm is None or mm.pos is None or self.kit.support or not me.get("alive", True):
            return None
        hp = float(me.get("hp_percent") or 0)
        gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
        if hp < 45:
            return None
        allies = list(mm.ally_champions)
        if self.jungle_state is not None:
            g = self._gank_plan(mm, allies, gt, me, now)
            if g is not None:
                return g
        sit = getattr(self, "situation", None)
        if sit is not None and sit.power_play and hp >= 50 and gt >= 300:
            # Two or more of them dead: the time to take something. Bots push after fights; Yasuo walked
            # back to farm (g18-g26).
            pp = None
            obj_t = (st.get("objectives") or {})
            left = sit.power_until - sit.game_s
            baron = map_places.places(self.side)["baron_pit"][0]
            near_baron = sum(1 for a in allies if dist(a, baron) < 3000)
            if gt >= 1200 and (obj_t.get("next_baron_in_s") or 0) <= 0 and len(sit.enemies_dead) >= 3 and left >= 30 \
                    and near_baron >= 2 and dist(mm.pos, baron) < 7000:
                pp = ("objective", baron, "power play: baron")   # three of them down for 30 s: the game-winning buff
            elif obj_t.get("herald_available") and left >= 25 and near_baron >= 1 and dist(mm.pos, baron) < 5000:
                pp = ("objective", baron, "power play: rift herald")
            pp = pp or macro.power_play_target(sit, mm.pos, mm, self.side, allies)
            if pp is None and (obj_t.get("next_dragon_in_s") or 0) <= 0 and sit.power_until - sit.game_s >= 25:
                pit = map_places.places(self.side)["dragon_pit"][0]
                if dist(mm.pos, pit) < 6000 and sum(1 for a in allies if dist(a, pit) < 3500) >= 1:
                    pp = ("objective", pit, "power play: dragon")
            if pp is not None:
                return pp
        j = self._join_fight_plan(mm, allies, gt, hp, now)
        if j is not None:
            return j
        if self.jungle_state is not None and self._levels_behind() >= 2:
            # A jungler two levels behind their team is worth more in his camps than at a dragon he
            # cannot contest: Lee spent six of g23's first 25 minutes on objectives and was level 8
            # to their 10-14.
            return None
        places = map_places.places(self.side)
        obj = st.get("objectives") or {}
        opp = st.get("lane_opponent") or {}
        if self.jungle_state is None and opp and not opp.get("alive", True) and float(opp.get("respawn_in_s") or 0) > 8:
            theirs = config.RED_TOWERS if self.side == "ORDER" else config.BLUE_TOWERS
            tower = min(theirs, key=lambda t: dist(mm.pos, t))
            wave_there = sum(1 for m in mm.ally_minions if dist(m, tower) < 1200)
            if dist(mm.pos, tower) < 3000 and wave_there >= 3:
                return ("push_tower", tower, "lane opponent dead and our wave at their tower: hit the tower")
        # Their team between me and it, or at it in greater numbers, and it is theirs: g33's Yasuo walked
        # to "dragon with allies" at 29:03 through two of them in our jungle and died 100% -> 0 in 5 s.
        foes_here = sum(1 for e in mm.enemy_champions if dist(e, mm.pos) < 1600)
        friends_here = sum(1 for a in allies if dist(a, mm.pos) < 1600)
        if foes_here >= 2 and foes_here > friends_here + 1:
            return None

        sit = getattr(self, "situation", None)
        fog = sit is not None and sit.unseen >= 3

        def outnumbered_at(pit, near: int) -> bool:
            # (Seen ones, and the fog: with three or more of them unseen, only with three of ours there;
            # g39 walked to "dragon with allies" at 19:00 and two came out of the fog, 100% -> 0 in 4 s.)
            return sum(1 for e in mm.enemy_champions if dist(e, pit) < 2000) > near + 1 or (fog and near < 3)

        if gt >= 300 and (obj.get("next_dragon_in_s") or 0) <= 0:
            pit = places["dragon_pit"][0]
            # In the pit, not merely on the bot side of the map: at 2500 units the bot lane standing
            # in lane counted, and Yasuo walked to an untouched dragon six times before 13:00 (g17).
            near = sum(1 for a in allies if dist(a, pit) < 1300)
            # A laner goes only from close by (mid, bot): top-lane Yasuo's 27 cross-map walks to it over
            # g38-g42 made 1 kill, 1 assist and 2 deaths, and left the wave each time.
            far = self.jungle_state is None and dist(mm.pos, pit) > 4500
            if (near >= 2 or (self.jungle_state is not None and near >= 1 and int(me.get("level") or 1) >= 6)) \
                    and not far and not outnumbered_at(pit, near):
                return ("objective", pit, "dragon with allies")
        if gt >= 1200 and (obj.get("next_baron_in_s") or 0) <= 0:
            pit = places["baron_pit"][0]
            near = sum(1 for a in allies if dist(a, pit) < 1500)
            if near >= 3 and not outnumbered_at(pit, near):
                return ("objective", pit, "baron with allies")
        if gt >= 1200 and len(allies) >= 3:
            best = max(allies, key=lambda a: sum(1 for b in allies if dist(a, b) < 2500))
            group = [b for b in allies if dist(best, b) < 2500]
            if len(group) >= 3 and dist(mm.pos, best) > 2500:
                cx = sum(b[0] for b in group) / len(group)
                cy = sum(b[1] for b in group) / len(group)
                return ("objective", (cx, cy), "group with the team")
        return None

    def _under_enemy_tower(self, pt) -> bool:
        """A map point inside the reach of one of their standing towers."""
        sit = getattr(self, "situation", None)
        towers = config.RED_TOWERS if self.side == "ORDER" else config.BLUE_TOWERS
        for i, t in enumerate(towers[:11]):
            if sit is not None and i < len(sit.their_towers) and not sit.their_towers[i]:
                continue
            if dist(pt, t) < config.TOWER_RANGE:
                return True
        return False

    def _join_fight_plan(self, mm, allies: list, gt: float, hp: float, now: float):
        """A skirmish close by: enemy champions next to ours on the minimap, within 3000 units of me,
        our side not outnumbered once I arrive. Kills happen in those fights (Ashe was 7/1 at 13:00 in
        g17 while Yasuo, 0/0/0, farmed mid). Sticky for 12 s; over when nobody is left fighting."""
        j = getattr(self, "_join", None)
        if j is not None:
            near = [e for e in mm.enemy_champions if dist(e, j["pt"]) < 1800]
            if near:
                j["pt"], j["seen"] = min(near, key=lambda e: dist(e, j["pt"])), now
            dive = self._under_enemy_tower(j["pt"]) and sum(1 for a in allies if dist(a, j["pt"]) < 1300) < 2
            if now > j["until"] or now - j["seen"] > 3 or hp < 50 or dive:   # (at 46% it still walked in, g31 13:00)
                self._join, self._join_next = None, now + 6.0
                return None
            return ("objective", j["pt"], "join the fight")
        if gt < 240 or hp < 70 or now < getattr(self, "_join_next", 0.0) or self._levels_behind() >= 2:
            return None  # (two levels behind their team, a skirmish is theirs: Lee died walking into them, g21)
            # (70%: joins from 55-70% went in half spent)
        sit = getattr(self, "situation", None)
        if sit is not None and sit.outnumbered:
            return None  # two or more of us dead: their five against our three wherever the fight goes
        best = None
        for e in mm.enemy_champions:
            d = dist(mm.pos, e)
            if not 1400 < d <= 3000:
                continue  # farther than ~9 s away, the fight is over on arrival (g17 wandered after 3700-3900 ones)
            friends = [a for a in allies if dist(a, e) < 1300]
            foes = [o for o in mm.enemy_champions if dist(o, e) < 1500]
            if not friends or len(friends) + 1 < len(foes):
                continue
            if len(friends) < 2 and self._under_enemy_tower(e):
                continue  # a fight at their tower is a dive: g33's Yasuo joined one on Shen there and died (6:41)
            if self.jungle_state is None and len(friends) < 2 and d > 2500:
                continue  # a laner leaves the wave for a real fight, not every 1-on-1 across the map (CS 30 at 21:00, g26)
            if len(friends) < 2 and sit is not None and sit.unseen >= 3 and not sit.power_play:
                # Three or more of them in the fog: a "1 of us on 1 of them" is where the rest of them are.
                # g40 (37:09) and g41 (30:15) went from 100% to dead in seconds walking into such fights.
                continue
            if self.jungle_state is None and gt < 840 and len(friends) < 2 and self.lane.project(e)[1] > 1200:
                # Laning: out of my lane only for a fight two of ours are already in. g39's top Yasuo walked to
                # "1 of us on 1 of them" fights in the river and mid twice before 10:00 and died both times
                # (their jungler found him there).
                continue
            score = d - 800 * len(friends)
            if best is None or score < best[0]:
                best = (score, e, len(friends), len(foes))
        if best is None:
            return None
        _, e, nf, ne = best
        self._join = {"pt": e, "seen": now, "until": now + 12.0}
        self.log_lines.append(f"objective: join the fight ({nf} of us on {ne} of them, {dist(mm.pos, e):.0f} away)")
        return ("objective", e, "join the fight")

    def _gank_plan(self, mm, allies: list, gt: float, me: dict, now: float):
        """A jungler's gank, by rule: an enemy laner past the middle of their lane (pushed toward our
        tower) within reach, where the numbers are not against us. The target point follows them on
        the minimap; the gank ends after 30 s, when they are lost for 5 s, when I drop under 40%, or
        when more of them gather than us. 45 s of camps between ganks."""
        g = getattr(self, "_gank", None)
        hp = float(me.get("hp_percent") or 0)
        if g is not None:
            near = [e for e in mm.enemy_champions if dist(e, g["pt"]) < 1500]
            if near:
                g["pt"], g["seen"] = min(near, key=lambda e: dist(e, g["pt"])), now
            many = sum(1 for e in mm.enemy_champions if dist(e, g["pt"]) < 2000)
            friends = sum(1 for a in allies if dist(a, g["pt"]) < 2500)
            there = dist(mm.pos, g["pt"]) < 1500
            if there:
                friends += 1  # Lee himself, once he is there: bot lane is always two of them
            # Only on the way: once Lee is there, what he sees decides (the fight layer), and the minimap
            # merges icons in a skirmish ("2 of them, 0 of us" ended 10 of g21's 19 ganks).
            bad = not there and ((many >= 2 and friends == 0) or many >= friends + 3)
            # The minimap counts jitter frame to frame (icons merge and split): bad numbers must hold for
            # 1.5 s before the gank is called off (it started with "2 of us" and ended the next second
            # on "0 of us", g25).
            g["bad_since"] = (g.get("bad_since") or now) if bad else None
            # Out of sight on the way is normal (they are in fog until we bring vision): lost only when
            # we are there and still see nobody (ganks were dropped mid-walk as "lost them", g25).
            lost = now - g["seen"] > 5 and dist(mm.pos, g["pt"]) < 1200
            if lost and not g.get("retargeted"):
                # There and she is gone: another of them still in our half of that lane is the gank now.
                ln = Lane(g["lane"], self.side)
                alt = [e for e in mm.enemy_champions if dist(e, g["pt"]) < 2500
                       and ln.project(e)[1] < 900 and ln.project(e)[0] <= ln.center]
                if alt:
                    g["pt"], g["seen"], g["retargeted"] = min(alt, key=lambda e: dist(e, mm.pos)), now, True
                    lost = False
            why = ("time" if now > g["until"] else "lost them" if lost else "low HP" if hp < 40
                   else f"{many} of them, {friends} of us" if bad and now - g["bad_since"] >= 3.0 else "")
            if why:
                self.log_lines.append(f"gank {g['lane']}: over ({why})")
                # A gank that found nothing cost 20-30 s of camps; five of those by 10:00 left Lee two
                # levels under their jungler (g23). Longer back to the camps after a miss.
                self._gank, self._gank_next = None, now + (75.0 if why in ("lost them", "time") else 45.0)
                return None
            return ("objective", self._gank_approach(g, mm), f"gank {g['lane']}")
        if gt < 195 or int(me.get("level") or 1) < 3 or hp < 60 or now < getattr(self, "_gank_next", 0.0):
            return None
        lanes = self._gank_lanes = getattr(self, "_gank_lanes", None) or {n: Lane(n, self.side) for n in ("top", "mid", "bot")}
        best = None
        for name, ln in lanes.items():
            for e in mm.enemy_champions:
                prog, off = ln.project(e)
                if off > 900 or prog > ln.center + ln.frac(400):
                    continue  # not on this lane, or not pushed toward our side
                many = sum(1 for o in mm.enemy_champions if dist(o, e) < 2000)  # the same radius that ends a gank
                friends = sum(1 for a in allies if dist(a, e) < 2000)
                if (many >= 2 and friends == 0) or many > friends + 1:
                    continue
                d = dist(mm.pos, e)
                if d > 4000 or (friends == 0 and d > 2000):
                    continue  # from 6400 units the walk took 18 s and they were gone (g21); g30 walked 3300-4300 units to laners at the middle
                if prog > ln.center - ln.frac(150):
                    continue  # at the middle she walks back before Lee arrives: g30's five ganks at 45-52% of the lane
                              # ended "lost them" or "time"; only a laner already in our half is worth the walk
                who = self._name_at(e, now)
                if who and who == self.whereabouts.jungler:
                    continue   # their jungler walking a lane is not a laner to gank
                jg_eta = self.whereabouts.jungler_eta(e, now)
                if jg_eta is not None and jg_eta < 8.0 and self.whereabouts.age(self.whereabouts.jungler, now) < 10.0:
                    continue   # their jungler was seen close enough to be there first: a counter-gank
                flash_down = bool(who) and self.enemy_flash_down(who, now)
                score = (d - 1500 * friends - ln.units(ln.center - prog)   # near, with our laner there, overextended
                         - (1500 if flash_down else 0))                       # "Flash down overrides everything"
                if best is None or score < best[0]:
                    best = (score, name, e, prog, friends)
        if best is None:
            return None
        _, name, e, prog, friends = best
        self._gank = {"lane": name, "pt": e, "seen": now, "until": now + 30.0}
        self.log_lines.append(f"gank {name}: their laner at {prog * 100:.0f}% of the lane, {friends} of us there, "
                              f"{dist(mm.pos, e):.0f} away")
        return ("objective", self._gank_approach(self._gank, mm), f"gank {name}")

    def _name_at(self, pt, now: float) -> str | None:
        """The enemy the minimap named at `pt` in the last second (iconid.py), or None."""
        best = None
        for name, (p, t) in self.whereabouts.seen.items():
            dd = dist(p, pt)
            if now - t <= 1.0 and dd < 400 and (best is None or dd < best[0]):
                best = (dd, name)
        return best[1] if best else None

    def _gank_approach(self, g: dict, mm):
        """Walk to where she will run, not to where she is: 500 units past her toward their tower
        while I am still far (half of the ganks ended "lost them": she saw Lee coming up the lane
        and walked home ahead of him). Close, straight at her."""
        if mm.pos is None or dist(mm.pos, g["pt"]) < 1600:
            return g["pt"]
        ln = Lane(g["lane"], self.side)
        prog, _ = ln.project(g["pt"])
        return ln.point(min(ln.center, prog + ln.frac(500)))

    def _do_objective(self, data: dict, ap: dict, stats: dict, now: float) -> None:
        """Walk to the objective answering fights on the way; there, fight and hit what is there
        (monsters count as targets, right-clicked; Smite for a jungler)."""
        m, mm = self.mech, self.mm_state
        pt, label = getattr(self, "_obj_pt", None), getattr(self, "_obj_label", "")
        if pt is None or m is None:
            return
        arrived = mm is not None and mm.pos is not None and dist(mm.pos, pt) < 1200
        monster = any(k in label for k in ("dragon", "baron", "herald"))
        if arrived and not monster:
            # A gank, a fight or a tower: not a camp. In camp mode the lane minions at the spot were the
            # "camp": Lee smote and cleared the enemy wave while the laner he came for walked off (g32,
            # a 30 s gank at mid of smite / attack camp / E on camp / Q2 on camp). Champions only here,
            # then on toward the spot (towers: attack-move onto them).
            if not self._micro_step(data, ap, stats, now, standing=label.startswith("power play")):
                m.go_map(pt, now, attack=not label.startswith("gank"), every=1.0)
        elif arrived:
            if self.micro is not None:
                self.micro.at_camp = True
            try:
                acted = self._micro_step(data, ap, stats, now, standing=True, camp_pt=pt, camp_big=True)
            finally:
                if self.micro is not None:
                    self.micro.at_camp = False
            if not acted:
                m.go_map(pt, now, attack=True, every=1.5)
        elif not self._micro_step(data, ap, stats, now, standing=False):
            m.go_map(pt, now, attack=False, every=1.0)
        m.last_action = f"objective: {label}"

    def _go_to(self, data: dict, ap: dict, stats: dict, now: float) -> None:
        """Travel to Jev's destination through the minimap, answering fights on the way with
        one-shot tactical moves. On arrival: a lane becomes the new lane to play; anywhere else
        the champion holds there, attack-moving onto it (objectives) between tactical moves."""
        m, d = self.mech, self.decision
        if m is None or d is None or not d.destination:
            return
        spots = map_places.places(self.side)
        if d.destination not in spots:
            return
        pt = spots[d.destination][0]
        pos = self.mm_state.pos if self.mm_state is not None else None
        arrived = pos is not None and math.dist(pos, pt) < 900
        if arrived and d.destination.endswith("_lane"):
            name = d.destination.split("_")[0]
            if name != self.lane.name and self._lane_switch_ok(now):
                self._switch_lane(name)
            self._micro_step(data, ap, stats, now) or m.go_map(pt, now, attack=True, every=1.5)
            return
        if self._micro_step(data, ap, stats, now, standing=arrived):
            return
        m.go_map(pt, now, attack=arrived, every=1.5 if arrived else 1.0)

    def _at_fountain(self, radius: float = 2600.0) -> bool | None:
        """True/False from the minimap position, None when the position is unknown. The radius is
        wide because in the map corner the camera stops at the map edge, so the view box centre
        (our position source) sits well away from the fountain even when we stand in it."""
        mmp = self.mm_state.pos if self.mm_state is not None else None
        if mmp is None:
            return None
        fountain = config.BLUE_FOUNTAIN if self.side == "ORDER" else config.RED_FOUNTAIN
        return math.dist(mmp, fountain) < radius

    def _lane_switch_ok(self, now: float) -> bool:
        """One lane switch per 10 s: Jev's "go to mid lane" and the wave farming switched top/mid and
        bot/mid three times a second in g34 (27 switches in 10 s)."""
        return now - getattr(self, "_lane_switched_at", 0.0) >= 10.0

    def _switch_lane(self, name: str) -> None:
        self._lane_switched_at = time.time()
        self.lane = Lane(name, self.side)
        if self.mech is not None:
            self.mech.lane = self.lane
            self.mech.nav.lane_units = self.lane.L
        # This lane's dead enemy towers, from what is known (the tower watch and the events), not an
        # empty set: a lane switch must not bring a dead tower back to life, or a standing one down.
        theirs = "red" if self.side == "ORDER" else "blue"
        ids = self.lane.enemy_tower_ids()
        self.dead_enemy_mid_towers = {ids[i] for t, i in getattr(self, "tower_watch", TowerWatch()).dead if t == theirs and i in ids}
        self._rehome_own_tower()
        self.log_lines.append(f"lane -> {name}")

    TOWER_NAMES = ["top outer", "top inner", "top inhibitor", "mid outer", "mid inner", "mid inhibitor",
                   "bot outer", "bot inner", "bot inhibitor", "nexus", "nexus"]

    def _situation(self, data: dict) -> "macro.Situation":
        """Who is alive, which towers stand, how many of them are unseen (macro.py)."""
        ours = "blue" if self.side == "ORDER" else "red"
        tw = self.tower_watch.dead
        dead_ours = {i for t, i in tw if t == ours}
        dead_theirs = {i for t, i in tw if t != ours} | getattr(self, "_dead_enemy_idx", set())
        me = find_me(data) or {}
        s = macro.analyze(data, self.mm_state, me.get("team") or ("ORDER" if self.side == "ORDER" else "CHAOS"), dead_ours, dead_theirs)
        prev = getattr(self, "situation", None)
        if s.power_play and not (prev is not None and prev.power_play):
            self.log_lines.append("macro: " + s.summary()["window"])
        # Pushed past the middle with three of them unseen is how the pushed-up deaths went (g19, g26):
        # the farm walk stops at the middle then, unless we are the ones with the numbers.
        if self.whereabouts.jungler is None:
            self.whereabouts.jungler = jungler_of(data.get("allPlayers", []), me.get("team") or self.side)
        if not self._enemy_names:
            self._enemy_names = [str(p.get("championName")) for p in data.get("allPlayers", [])
                                 if p.get("team") and p.get("team") != me.get("team") and p.get("championName")]
        now = time.time()
        threat = self._jungler_threat(now)
        if threat != getattr(self, "_jungler_threat_logged", None) and now - getattr(self, "_wa_logged", 0.0) > 5.0:
            self._jungler_threat_logged, self._wa_logged = threat, now
            w = self.whereabouts
            if w.jungler and w.jungler in w.seen:
                desc = w.describe(self.mm_state.pos if self.mm_state else None, now, map_places.places(self.side))
                self.log_lines.append(f"whereabouts ({threat or 'unknown'}): {w.jungler} (jungler) "
                                      f"{desc.get(w.jungler + ' (jungler)', '')}")
        if self.mech is not None:
            # (Four or more: the minimap shows two or fewer enemy icons most of the game, so "three unseen"
            # was 62-81% of the g22-g29 frames and would have kept Yasuo on our half all game.)
            cap = (self.lane.center + self.lane.frac(350)) if (s.unseen >= 4 and not s.power_play) else None
            gank = self._jungler_threat(time.time())
            if gank == "far":
                cap = None   # their jungler was seen far away moments ago: the unseen count is not a gank
            elif gank == "near" and not s.power_play:
                cap = self.lane.center   # unseen, and could be on me already: not past the middle
            self.mech.mia_cap = cap
        return s

    def _jungler_threat(self, now: float) -> str | None:
        """Their jungler by his last minimap sighting: "far" (seen in the last 25 s and at least 12 s
        from me), "near" (not in sight for 2 s+, and could be on me already), or None (on the map
        now, or not seen for a while: the unseen count stands)."""
        w, mm = self.whereabouts, self.mm_state
        if not w.jungler or mm is None or mm.pos is None:
            return None
        age = w.age(w.jungler, now)
        eta = w.jungler_eta(mm.pos, now)
        if eta is None or age is None:
            return None
        if eta >= 12.0:
            return "far"
        if age >= 2.0 and eta <= 0.0:
            return "near"
        return None

    def _towers_from_minimap(self) -> None:
        """Towers the minimap shows gone (TowerWatch): ours move our tower line back (retreats and holds
        go behind the frontmost one standing); theirs stop counting as a threat."""
        from jev.lanes import LANE_TOWER_BASE

        ours = "blue" if self.side == "ORDER" else "red"
        while self._towers_seen_dead:
            team, i = self._towers_seen_dead.popleft()
            if i >= 9:  # a nexus tower: the situation reads it from the watch; no lane line to move
                self.log_lines.append(f"towers: {'our' if team == ours else 'their'} nexus tower is down")
                continue
            ln = ("top", "mid", "bot")[i // 3]
            num = (5, 4, 3)[i % 3]
            if team == ours:
                tag = "T1" if self.side == "ORDER" else "T2"
                self._dead_turrets = getattr(self, "_dead_turrets", set())
                self._dead_turrets.add(f"Turret_{tag}_{LANE_LETTER[ln]}_{num:02d}_A")
                self.log_lines.append(f"towers (minimap): our {self.TOWER_NAMES[i]} is down")
                self._rehome_own_tower()
            else:
                if i in self.lane.enemy_tower_ids():
                    self.dead_enemy_mid_towers.add(self.lane.enemy_tower_ids()[i])
                self._dead_enemy_idx = getattr(self, "_dead_enemy_idx", set()) | {i}
                self.log_lines.append(f"towers (minimap): their {self.TOWER_NAMES[i]} is down")

    def _rehome_own_tower(self) -> None:
        """Our tower in this lane is the frontmost one still standing: retreats, step-backs and holds
        go behind it. With the outer tower gone, Yasuo "retreated" to its ruins in the middle of the
        enemy wave and died there, three times (g16)."""
        from jev.lanes import LANE_TOWER_BASE

        ln = self.lane
        own = config.BLUE_TOWERS if self.side == "ORDER" else config.RED_TOWERS
        tag = "T1" if self.side == "ORDER" else "T2"
        dead = getattr(self, "_dead_turrets", set())
        base = LANE_TOWER_BASE[ln.name]
        for k, num in enumerate((5, 4, 3)):
            if f"Turret_{tag}_{LANE_LETTER[ln.name]}_{num:02d}_A" not in dead:
                prog = ln.project(own[base + k])[0] + ln.frac(200)
                break
        else:
            prog = ln.frac(1800)  # the nexus towers: just outside our base
        if abs(prog - ln.own_tower) > 1e-6:
            ln.own_tower = prog
            ln.max_advance = min(ln.max_advance, ln.center - ln.frac(450))
            self.log_lines.append(f"towers: our {ln.name} line is now at {prog:.0%} of the lane")

    def _metrics(self) -> dict:
        """Outcome signals for the decision log."""
        me = self.state.get("me", {}) if self.state else {}
        k, dd, a = (int(x) for x in str(me.get("kda", "0/0/0")).split("/")) if me.get("kda") else (0, 0, 0)
        sc = self.scene
        return {"hp": me.get("hp_percent", 0) or 0, "gold": me.get("gold", 0) or 0, "cs": me.get("cs", 0) or 0,
                "kills": k, "deaths": dd, "assists": a,
                "enemy_hp": sc.champ.unit.hp if sc is not None and sc.champ is not None else None}

    # -- render -------------------------------------------------------------------------
    def _table(self, perception: Perception) -> Table:
        t = Table(title=f"league-of-jev  ({'DRY RUN' if self.dry_run else 'LIVE'})", expand=True)
        t.add_column("field")
        t.add_column("value")
        me = self.state.get("me", {})
        t.add_row("game", str(self.state.get("game", {}).get("time")))
        t.add_row("me", f"{me.get('champion')} L{me.get('level')} HP {me.get('hp_percent')}% gold {me.get('gold')} {me.get('kda')} cs {me.get('cs')}")
        t.add_row("opponent", escape(json.dumps(self.state.get("lane_opponent"))))
        t.add_row("where", f"{perception.position} lane {perception.lane_progress_pct}% dmg {perception.hp_lost_recent_pct:.0f}%")
        t.add_row("jev", escape(self.decision.summary()) if self.decision else "waiting")
        t.add_row("intent", self.intent)
        t.add_row("keys", escape(self.kb.describe()))
        t.add_row("actions", escape("\n".join(self.log_lines)))
        return t

    # -- main -----------------------------------------------------------------------------
    def run(self) -> None:
        console.print(f"keybinds: {self.kb.describe()}", markup=False)
        while True:
            self._run_one_game()
            if not self.forever:
                return
            self._reset_for_next_game()

    def _reset_for_next_game(self) -> None:
        self.decision = None
        self.state = {}
        self.phase = "base"
        self.intent = "farm"
        self._base_shop_done = False
        self.guards = Guards()
        self.hp = HpTracker(config.TIMING.damage_window_s)
        self.dead_enemy_mid_towers = set()
        self.jungle_state = None
        self.mm_state = None
        self.mm_filter = PosFilter()
        self.tower_watch = TowerWatch()
        self._towers_seen_dead.clear()
        self._seen_names.clear()
        self.view = None
        self.scene = None
        self.min_tracker = UnitTracker()
        self.champ_tracker = UnitTracker(max_jump_px=90, blink_px=480 * config.VISION.px_per_unit)
        self._gold_hist.clear()
        self._stop = threading.Event()
        self.recorder = FixtureRecorder()

    def _run_one_game(self) -> None:
        console.print("waiting for a game...")
        while not self.riot.is_game_running():
            time.sleep(1)
        data = self.riot.all_game_data() or {}
        me = find_me(data) or {}
        side = me.get("team", "ORDER")
        self.side = side
        # Kit from the champion actually in the game; role from the assigned position (empty in
        # custom games, where the kit's default role applies); lane from the role.
        role = self.role_override or str(me.get("position") or "")
        self.kit = kit_for(self.champion_override or me.get("championName", "Yasuo"), role)
        names = actions.summoner_names(me)
        if getattr(self.kit, "jungle", False) and all(names) and "smite" not in names:
            # No Smite, so no pet: camps hit a jungler several times harder, the bleeding floor walks him
            # off each one before it dies and it resets (g34's Lee was level 1 at 6:00). Lane top instead.
            # (Only with both spells read: at 0:00 the player list can be empty, g20.)
            import dataclasses

            self.kit = kit_for(self.champion_override or me.get("championName", "Yasuo"), "TOP")
            self.kit.items = dataclasses.replace(self.kit.items, starters=["Doran's Blade", "Health Potion"])
            self.log_lines.append("jungle: no Smite this game: laning top instead, starting with Doran's Blade")
        self.role = self.kit.role
        self.lane = Lane(lane_for(self.kit.role, "bot" if self.kit.support else "mid"), side)
        self.mech = Mechanics(self.ctl, self.screen, self.kb, side, lane=self.lane, skill_order=self.kit.skill_order)
        self.mech.blind_q_enabled = self.vision is None
        self.micro = Micro(self.ctl, self.screen, self.kb, side)
        self.micro.on_fight_order = lambda what: self.log_lines.append(f"order: {what}")
        # Learns the lane planner's numbers during the game (lanelearn.py); dry runs start clean.
        self.micro.learner = LaneLearner(path=Path("logs/lane_learned.json") if not self.ctl.dry_run else Path("/dev/null/x"))
        self.jungle_state = JungleState(side) if getattr(self.kit, "jungle", False) else None
        if self.jungle_state is not None and self.logfile:
            try:
                with open(f"{self.logfile}.jungle.json") as fh:
                    saved = json.load(fh)
                gt0 = float((data.get("gameData") or {}).get("gameTime", 0.0))
                if all(t <= gt0 + 5 for t in saved.get("cleared", {}).values()):  # same game, restarted
                    self.jungle_state.cleared = {k: float(v) for k, v in saved["cleared"].items()}
                    self.jungle_state.route_i = int(saved.get("route_i", 0))
                    self.log_lines.append(f"jungle: resumed timers {self.jungle_state.cleared}")
            except (OSError, ValueError, KeyError):
                pass
        if self.shop_brain is not None:
            self.shop_brain.profile = self.kit.items
        self.data = data
        self._api_dead = False
        console.print(f"game found, side {side}, champion {me.get('championName')} as {self.kit.name} {self.kit.role}, lane {self.lane.name}")
        self._camera_checked = False
        gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
        if gt > 90 and not me.get("isDead"):
            # Joined mid-game: position unknown, so walk to the own tower first and re-base there.
            self.phase = "lane"
            self.guards.left_base_at = time.time()
            self.guards.resync_until = time.time() + config.TIMING.resync_s
            self.mech.resync_to_own_tower()
            self._base_shop_done = True
        threading.Thread(target=self._brain_loop, daemon=True).start()
        threading.Thread(target=self._api_loop, daemon=True).start()
        threading.Thread(target=self._build_loop, daemon=True).start()
        if not self.dry_run:
            threading.Thread(target=self._perceive_loop, daemon=True).start()
            if self.tactic_hz > 0:
                # The tactical head only for kits without the lane planner: Yasuo's planner and combos
                # decide every order, and g35's 1,984 tactical calls were acted on 19 times (1%) while
                # being the largest share of the API bill. Lee's jungle uses it (24% acted on, g32).
                if not (config.FAST.lane_planner and hasattr(self.kit, "plan_step")):
                    self.tactics = TacticalBrain(max_hz=self.tactic_hz, explore=self.explore)
                    self.tactics.on_answer = self._wake_actor.set
                    threading.Thread(target=self.tactics.run, daemon=True).start()
                else:
                    self.log_lines.append("tactics head off: the lane planner decides this kit's orders")
                self.fights = FightBrain(self.kit.role_text(self.lane.name),
                                         log_path=f"{self.logfile}.fights.jsonl" if self.logfile else None)
                threading.Thread(target=self.fights.run, daemon=True).start()
        tick = 1 / (config.FAST.act_hz if not self.dry_run else config.TIMING.tick_hz)
        perception = Perception()
        try:
            with Live(self._table(perception), refresh_per_second=4, console=console) as live:
                while True:
                    t0 = time.time()
                    data = self.data if not self.dry_run else self.riot.all_game_data()
                    if self._api_dead or (data is None and self.dry_run):
                        break
                    if data is None:
                        time.sleep(0.2)
                        continue
                    if not self.dry_run and not self.ctl.keys_ok():
                        if self.keep_front and t0 - self._last_activate > 3.0:
                            self._last_activate = t0
                            activate_game()
                            time.sleep(0.4)
                        if not self.ctl.keys_ok():
                            self.paused = True
                            from jev.control import system_prompt_on_screen

                            prompt = system_prompt_on_screen()
                            if prompt and t0 - getattr(self, "_prompt_logged", 0.0) > 60:
                                self._prompt_logged = t0
                                self.log_lines.append(f"input paused: a system prompt ({prompt}) is on screen; not clicking it")
                            perception = Perception(position=f"paused: {prompt} prompt on screen" if prompt else "paused: game window not active")
                            self.state = build_state(data, perception, self.role, self._opp_hint())
                            live.update(self._table(perception))
                            self._logline(perception, t0)
                            time.sleep(0.5)
                            continue
                    if self.paused:
                        self.paused = False
                        if self.mech:
                            self.mech._last_move = 0.0
                    # Watchdog: an iteration stuck for 30 s dumps every thread's stack to the console (g33 froze
                    # at 17:21 for two minutes with no error; the next one names its culprit).
                    faulthandler.dump_traceback_later(30.0, exit=False)
                    try:
                        self._pause_guard(data, t0)
                        self._dismiss_dialog(t0)
                        self._close_stray_shop(t0)
                        self._lasthit_check(float((data.get("activePlayer") or {}).get("currentGold", 0.0)), t0)
                        perception = self._tick(data, t0)
                    finally:
                        faulthandler.cancel_dump_traceback_later()
                    self.state = self._full_state(data, perception)
                    self.dlog.resolve(self._metrics())
                    if self.fights is not None:
                        self.fights.log.resolve(self._metrics())
                    if t0 - self._last_table > 0.25:
                        self._last_table = t0
                        live.update(self._table(perception))
                    self._logline(perception, t0)
                    # Act again on the next frame or Jev answer, or after one tick at the latest.
                    self._wake_actor.wait(timeout=max(0.0, tick - (time.time() - t0)))
                    self._wake_actor.clear()
        finally:
            self._stop.set()
            if self.tactics is not None:
                self.tactics.stop()
            if self.fights is not None:
                self.fights.stop()
        console.print("game over")

    def _gank_converging(self, mmg, now: float) -> list:
        """Two or more of them near me on the minimap with none of ours, converging: the second of them
        within 1600 units, or 200+ units nearer than a second ago. "Two within 2200" alone fired 43 times
        in g39 (their laner in lane, their jungler at his camps): 170 s of retreating at 80-100% HP.
        Returns those within 2200, or [] when it is not a gank."""
        foes = [e for e in mmg.enemy_champions if dist(mmg.pos, e) < 2200]
        friends = [a for a in mmg.ally_champions if dist(mmg.pos, a) < 2200]
        ds = sorted(dist(mmg.pos, e) for e in foes)
        hist = self._foe_hist = getattr(self, "_foe_hist", collections.deque(maxlen=40))
        hist.append((now, ds))
        before = next((d for t, d in hist if 0.8 <= now - t <= 1.5 and len(d) >= 2), None)
        closing = len(ds) >= 2 and before is not None and before[1] - ds[1] >= 200
        return foes if len(foes) >= 2 and not friends and (ds[1] <= 1600 or closing) else []

    def _tick(self, data: dict, now: float) -> Perception:
        m = self.mech
        assert m is not None
        ap = data.get("activePlayer", {})
        cs = ap.get("championStats", {})
        hp_pct = 100 * cs.get("currentHealth", 0) / max(cs.get("maxHealth", 1), 1)
        # The fountain speed buff reports 700+; cap so dead reckoning does not sprint ahead of the champion.
        move_speed = min(float(cs.get("moveSpeed", 345)), config.TIMING.max_reckon_speed)
        me = find_me(data) or {}
        lost = self.hp.update(hp_pct, now)
        if not self._self_trail or now - self._self_trail[-1][0] >= 0.1:
            self._self_trail.append((now, hp_pct))
        mmg = self.mm_state
        if (mmg is not None and mmg.pos is not None and now - mmg.ts < 1.0 and now >= self.guards.retreat_until
                and self.jungle_state is None and self.micro is not None and self.micro.mode not in FIGHT_MODES):
            foes = self._gank_converging(mmg, now)
            if foes:
                # Two of them converging on the minimap and none of us: the gank that killed Yasuo in
                # g08 and g11 showed there seconds before it arrived.
                self.guards.retreat_until = now + 3.0
                self.log_lines.append(f"gank: {len(foes)} enemy champions within 2200 on the minimap, no ally: retreat")
        bleed = self._trail_change(3.0, now)
        fr = self.fights.read if self.fights is not None else None
        winning = fr is not None and now - fr.ts < 1.0 and fr.plan == "all_in" and fr.win_all_in >= 0.6
        unseen = self.scene is None or self.scene.champ is None
        if (bleed is not None and ((bleed <= -12 and hp_pct < 70 and unseen) or (bleed <= -15 and hp_pct < 60 and not winning))
                and now >= self.guards.retreat_until and (now >= self.guards.fight_back_until or hp_pct < 30)):
            # Hit from off screen (a ranged champion past the screen edge): Yasuo bled 60% -> 0 over
            # twelve seconds while holding the wave, no champion ever on screen (g13, 7:25).
            self.guards.retreat_until = now + 4.0
            self.log_lines.append(f"bleeding ({bleed:.0f}% in 3 s{', nothing in view' if unseen else ''}): retreat")
        self._hp_now, self._hp_lost = hp_pct, lost
        # Significant change: new damage, level, or death state -> wake the brain right away.
        sig = (int(ap.get("level", 1)), bool(me.get("isDead")), int(hp_pct // 10))
        if lost >= 8 or sig != self._last_sig:
            self._last_sig = sig
            self._wake.set()
        p = Perception(
            position=self.phase,
            lane_progress_pct=m.nav.pct,
            hp_lost_recent_pct=lost,
            seconds_since_damage=self.hp.since_damage(now),
            recalling=m.recall_started is not None,
        )
        wave = None
        if self.mm is not None:
            mm = self.mm_state  # read by the perception thread
            if mm is not None and mm.pos is not None and now - mm.ts < 1.0:
                prog, lat = self.lane.project(mm.pos)
                m.nav.progress = prog
                m.nav._last_t = now
                p.lane_progress_pct = m.nav.pct
                wave = lane_wave(mm, self.lane)
                p.enemy_champions_on_minimap = len(mm.enemy_champions)
                p.enemy_minions_in_lane, p.ally_minions_in_lane = wave.enemy_count, wave.ally_count
                if mm.enemy_champions:
                    p.nearest_enemy_champion_units = min(dist(mm.pos, e) for e in mm.enemy_champions)
                if wave.enemy_front is not None:
                    p.wave_position = self.lane.wave_label(wave.enemy_front)
                # Tower safety: enemy towers of this lane that still stand (all others count too).
                enemy_towers = config.RED_TOWERS if self.side == "ORDER" else config.BLUE_TOWERS
                lane_ids = self.lane.enemy_tower_ids()  # index in list -> tower number in event names
                self._enemy_tower_map = None
                for i, t in enumerate(enemy_towers):
                    if i in lane_ids and lane_ids[i] in self.dead_enemy_mid_towers:
                        continue
                    if dist(mm.pos, t) < 1700 and (self._enemy_tower_map is None or dist(mm.pos, t) < dist(mm.pos, self._enemy_tower_map)):
                        self._enemy_tower_map = t  # the lane planner keeps its spots out of its range
                    if dist(mm.pos, t) < config.TOWER_RANGE:
                        p.near_enemy_tower = True
                        p.ally_minions_at_enemy_tower = sum(1 for a in mm.ally_minions if dist(a, t) < 900)
                        break
                self._near_enemy_tower = p.near_enemy_tower
                self._tower_farm_ok = p.ally_minions_at_enemy_tower >= 3 and hp_pct >= 50
                if self.micro is not None:
                    self.micro.near_enemy_tower = p.near_enemy_tower
                # Q aim: nearest enemy champion, else nearest enemy minion, within reach.
                targets = mm.enemy_champions or mm.enemy_minions
                if targets:
                    tgt = min(targets, key=lambda e: dist(mm.pos, e))
                    d = dist(mm.pos, tgt)
                    if d < 900:
                        dx, dy = tgt[0] - mm.pos[0], tgt[1] - mm.pos[1]
                        m.aim_dir = (dx / d, -dy / d)
                    else:
                        m.aim_dir = None
                else:
                    m.aim_dir = None
        # Track destroyed towers from the event log, and from the minimap icons (the event names never
        # matched ours in g22-g26: every real name is logged once so the format can be checked).
        enemy_tag = "T2" if self.side == "ORDER" else "T1"
        self._towers_from_minimap()
        for e in (data.get("events") or {}).get("Events", []):
            tk = str(e.get("TurretKilled", ""))
            if e.get("EventName") == "TurretKilled" and tk:
                self._dead_turrets = getattr(self, "_dead_turrets", set())
                if tk not in self._dead_turrets:
                    self._dead_turrets.add(tk)
                    self.log_lines.append(f"tower killed (event): {tk}")
                    key = tower_from_event(tk)
                    if key is not None and key not in self.tower_watch.dead:
                        # The event is exact and immediate: the minimap watch's answer, now.
                        self.tower_watch.dead.add(key)
                        self._towers_seen_dead.append(key)
                        self._towers_from_minimap()
                    self._rehome_own_tower()
            if e.get("EventName") == "TurretKilled" and f"Turret_{enemy_tag}_{LANE_LETTER[self.lane.name]}_" in tk:
                try:
                    self.dead_enemy_mid_towers.add(int(tk.split("_")[3]))
                except (IndexError, ValueError):
                    pass

        # Level-ups: the kit's skill order, R at 6/11/16. Jev's pick put W at 2 and E at 6 (no R) in
        # g29, and E came at level 4: no E+Q and no ultimate in lane.
        levels = {k: ap.get("abilities", {}).get(k, {}).get("abilityLevel", 0) for k in ("Q", "W", "E", "R")}
        level = int(ap.get("level", 1))
        if level != self._level_seen:
            self._level_seen, self._level_changed_at = level, now
        if sum(levels.values()) < level and now - self.guards.last_level_t > 1.0 and now - self._level_changed_at > 0.3:
            self.guards.last_level_t = now
            ab = m.level_up(levels, legal_level_ups(level, levels))
            if ab:
                self.log_lines.append(f"level {ab} (rank {levels.get(ab, 0) + 1}, at level {level})")

        if me.get("isDead"):
            if self.phase != "dead":
                m.cancel_recall()
                self._build_wake.set()
            self.phase = "dead"
            m.nav.reset_to_base()
            self.intent = "dead"
            self._base_shop_done = False
            return p
        if self.phase == "dead":
            self.phase = "base"
            self._camera_checked = False

        # The camera must follow us. Typed shop searches that miss the search box reach the game as
        # hotkeys ("Cloak of Agility" ends in Y, the camera-lock toggle): the camera stayed on our
        # fountain, the minimap box said we were home, and Yasuo stood in a shop loop for three
        # minutes (g16). No self bar on screen for 2.5 s while alive: check the lock again.
        v = self.view
        if v is not None and now - v.ts < 0.6 and v.me is None and hp_pct > 0 and m.recall_started is None:
            self._me_missing_since = getattr(self, "_me_missing_since", None) or now
        else:
            self._me_missing_since = None
        if ((self._me_missing_since is not None and now - self._me_missing_since > 2.5)
                or now >= getattr(self, "_camera_recheck_at", float("inf"))) and now - getattr(self, "_cam_fix_t", 0.0) > 3.0:
            # Recenter with a tap of the camera-snap key, which cannot hurt a locked camera. The lock
            # check that ran here toggled the lock on a guess: a locked camera toggled off stays where
            # it is and read as "now locked", and the bar goes missing for other reasons (dashes,
            # effects), so g26 ran 135 checks and 5 toggles and ended with 50 CS.
            self._cam_fix_t, self._camera_recheck_at = now, float("inf")
            if self.ctl.keys_ok():
                self.ctl.hold(self.kb.camera_snap, True)
                time.sleep(0.12)
                self.ctl.hold(self.kb.camera_snap, False)
            if now - getattr(self, "_cam_fix_logged", 0.0) > 60:
                self._cam_fix_logged = now
                self.log_lines.append("camera: my bar is gone: recentering (camera-snap tap)")
        if not getattr(self, "_camera_checked", False) and self.mm is not None and self.ctl.keys_ok():
            self._camera_checked = True
            note = m.ensure_camera_locked(self.mm)
            self.log_lines.append(note)

        # Standing in our fountain with gold to spend (by the minimap, whatever the phase says):
        # shop once per visit. Covers restarts mid-game, respawns and recalls alike.
        mmp = self.mm_state.pos if self.mm_state is not None else None
        fountain = config.BLUE_FOUNTAIN if self.side == "ORDER" else config.RED_FOUNTAIN
        if mmp is not None:
            d_f = math.dist(mmp, fountain)
            if d_f > 3000:
                self._fountain_shopped = False
                self._shop_fails = {}
            elif (d_f < 2400 and not getattr(self, "_fountain_shopped", False) and float(ap.get("currentGold", 0)) >= 75
                  and (self.dry_run or self.ctl.keys_ok()) and now - getattr(self, "_fountain_try_t", 0.0) > 5.0):
                # Retry every 5 s while in the fountain until something is bought (a first attempt
                # during the loading hand-off failed and was never retried).
                self._fountain_try_t = now
                gold0 = float(ap.get("currentGold", 0))
                self._shop_if_possible(gold0)
                fresh = self.riot.all_game_data() or {}
                if float((fresh.get("activePlayer") or {}).get("currentGold", gold0)) < gold0 - 40:
                    self._fountain_shopped = True
                self._base_shop_done = True
        # Shop once per visit to base: at game start, after respawn, after a recall.
        if self.phase == "base" and not self._base_shop_done:
            gold = float(ap.get("currentGold", 0))
            if not hasattr(self, "_base_since"):
                self._base_since = now
            fresh = self.build is not None and now - self.build.ts < 6.0
            if self.shop_brain is not None and not fresh and now - self._base_since < 3.0:
                self._build_wake.set()
                return p  # give the build head a moment to re-plan with the current gold
            if self._at_fountain() is False and now - self._base_since < 6.0:
                return p  # not in shop range yet (recall still landing): wait
            if not self.dry_run and not self.ctl.keys_ok():
                return p  # input cannot reach the game yet (loading / not in front): shop once it can
            if gold >= 50 and self._at_fountain() is True:
                # Only where the minimap confirms the fountain: with the position unknown the shop
                # opened in lane, queued an item, and stayed open over the game (game 2).
                self._shop_if_possible(gold)
            self._base_shop_done = True
            del self._base_since

        self.situation = self._situation(data)
        state = self.state or build_state(data, p, self.role, self._opp_hint())
        self.intent = choose_intent(self.decision, state, p, now, self.guards)
        d0 = self.decision
        if self.intent == "retreat" and d0 is not None and d0.intent == "retreat" and now >= self.guards.retreat_until:
            # Jev's own retreat call at high HP with nothing hitting us cost game 4 a quarter of its
            # lane time (70% of retreat seconds were at 60-100% HP). Keep it only with real danger.
            mm = self.mm_state
            near = sum(1 for e in (mm.enemy_champions if mm is not None and mm.pos is not None else [])
                       if dist(mm.pos, e) < 2000)
            if hp_pct >= 55 and lost < 8 and d0.danger < 1.8 and near < 2:
                self.intent = "farm"
        if self.intent in ("go_to", "group") and d0 is not None and d0.intent_probabilities.get(self.intent, 0.0) < 0.35:
            self.intent = "farm"  # a low-confidence roam costs a laner CS and exposes them; keep laning
        sit = getattr(self, "situation", None)
        if (self.intent == "recall" and sit is not None and sit.power_play and hp_pct >= 55
                and m.recall_started is None):   # (junglers too: Lee recalled at 90% in g30's power play)
            # Two or more of them dead: a tower or the dragon now, the shop after (the window closes in
            # seconds; the gold keeps). The objective plan below picks the target.
            self.intent = "farm"
        if self.intent in ("farm", "go_to", "group"):
            plan_obj = self._objective_plan(data, now)
            if plan_obj is not None:
                kind, pt, label = plan_obj
                if kind == "push_tower":
                    self.intent = "push_tower"
                    self.guards.push_ok_until = now + 1.5
                else:
                    self.intent = "objective"
                    self._obj_pt, self._obj_label = pt, label
                seen = self._obj_logged_at = getattr(self, "_obj_logged_at", {})
                if now - seen.get(label, 0.0) > 30.0:  # labels alternate: once per label per 30 s
                    seen[label] = now
                    self.log_lines.append(f"objective: {label}")
        if self.kit.support and self.intent in ("go_to", "group", "trade", "all_in", "push_tower"):
            self.intent = "farm"  # support: stay with the carry (shadow them) instead of roaming or engaging alone
        if self.intent == "recall" and now - self.guards.left_base_at < config.TIMING.no_recall_after_base_s:
            self.intent = "farm"
        if self.intent == "recall" and self._at_fountain():
            # Already home: recalling again is a loop. Shop (at most every 10 s) and head out.
            self.intent = "farm"
            if now - getattr(self, "_fountain_retry_t", 0.0) > 10.0 and float(ap.get("currentGold", 0)) >= 300:
                self._fountain_retry_t = now
                self._shop_if_possible(float(ap.get("currentGold", 0)))

        if m.recall_started is not None:
            if lost > 1.0:
                m.cancel_recall()
                self.intent = "retreat"
            elif m.recall_done(now) and (self._at_fountain() is not False or now - m.recall_started > 11.0):
                m.cancel_recall()
                m.nav.reset_to_base()
                self._build_wake.set()
                self.phase = "base"
                self._base_shop_done = False
                self.intent = "farm"
            else:
                return p

        jdest = self.decision.destination if self.decision is not None else None
        if self.intent in ("go_to", "group") and jdest in ("dragon_pit", "baron_pit"):
            # An objective needs the team there: alone, a level-3 Lee attack-moved in the dragon pit
            # for two minutes (g07) and the bots never came. Go only when two allies are near it.
            pit = map_places.places(self.side)[jdest][0]
            mm = self.mm_state
            allies_near = sum(1 for a in (mm.ally_champions if mm is not None else []) if dist(a, pit) < 2500)
            if allies_near < 2:
                self.intent = "farm"
        if self.jungle_state is not None and self.intent in ("go_to", "group"):
            gt_now = float((data.get("gameData") or {}).get("gameTime", 0.0))
            p_go = d0.intent_probabilities.get(self.intent, 0.0) if d0 is not None else 0.0
            if int(ap.get("level", 1)) < 3 or gt_now < 195 or p_go < 0.5:
                # First clear before any roam (level 1 Lee walked to mid at 1:51 with no camp taken),
                # and later only a confident call pulls the jungler off his camps.
                self.intent = "farm"
        if self.jungle_state is not None and self.intent in ("go_to", "group") and (
                jdest in ("my_red_buff", "my_blue_buff") or not jdest or "jungle" in str(jdest)):
            # A jungler sent to its own buff (or nowhere in particular) clears camps rather than just
            # walking there; ganks and objectives (lanes, dragon, baron) still go through go_to.
            if jdest in ("my_red_buff", "my_blue_buff"):
                want = "red" if jdest == "my_red_buff" else "blue"
                gt_now = float((data.get("gameData") or {}).get("gameTime", 0.0))
                if self.jungle_state.current != want and self.jungle_state.up(want, gt_now):
                    self.jungle_state.current, self.jungle_state.arrived_at = want, None
            self.intent = "farm"
        if self.jungle_state is not None and self.intent in ("farm", "trade", "push_tower", "defend"):
            if self.phase == "base" and self._at_fountain() is False:
                self.phase = "lane"
                self.guards.left_base_at = now
            p.position = "jungle"
            self._jungle_step(data, ap, cs, now)
            return p
        if self.phase == "base":
            m.go_lane(move_speed, now)
            if m.nav.progress >= self.lane.own_tower:
                self.phase = "lane"
                self.guards.left_base_at = now
            p.position = "traveling"
            return p

        p.position = "lane"
        if now < self.guards.resync_until and (self.mm_state is None or self.mm_state.pos is None):
            m.retreat(move_speed, now)  # walking to own tower to re-base position
            self.intent = "resync"
            return p
        if self.intent == "objective":
            self._do_objective(data, ap, cs, now)
            return p
        dest = self.decision.destination if self.decision is not None else None
        if dest and (self.intent in ("go_to", "group") or (self.intent == "defend" and dest.startswith("my_"))):
            self._go_to(data, ap, cs, now)
            return p
        if self.intent in ("farm", "trade", "push_tower", "defend") and self._micro_step(data, ap, cs, now):
            return p
        if self.intent == "farm" and self.kit.support:
            # Support macro: shadow the carry (the allied champion nearest this lane on the minimap),
            # a little behind them and never past our side of the lane; hold at our tower without one.
            ln, target = self.lane, self.lane.own_tower
            mm = self.mm_state
            if mm is not None and mm.ally_champions:
                near = [(ln.project(a), a) for a in mm.ally_champions]
                near = [pr for (pr, d), a in near if d < 1500]
                if near:
                    target = max(near) - ln.frac(250)
            target = max(ln.own_tower - ln.frac(300), min(target, ln.center - ln.frac(300)))
            m.go_progress(target, move_speed, now, attack=False)
            m.last_action = f"support: shadow carry at lane {int(target * 100)}%"
            return p
        if self.intent == "farm" and self._farm_the_waves(now):
            return p
        if self.intent == "farm":
            contact = self._income_contact(float(ap.get("currentGold", 0)), int(me.get("scores", {}).get("creepScore", 0)), now) \
                or 0.5 <= lost < config.TIMING.heavy_damage_pct
            m.farm(move_speed, now, self.decision.aggression if self.decision else 1.0, contact, wave)
        elif self.intent == "trade":
            m.trade(move_speed, now)
        elif self.intent == "push_tower":
            m.push(move_speed, now)
        elif self.intent == "retreat":
            if not self._micro_step(data, ap, cs, now, standing=False, escaping=True):
                if self.jungle_state is not None:
                    # A jungler's way out is through the jungle, not down the mid lane: to our nearest
                    # standing tower, or home when low. Walking to the fountain on every short retreat
                    # cost Lee two minutes of camps by 25:00 (g23).
                    home = config.BLUE_FOUNTAIN if self.side == "ORDER" else config.RED_FOUNTAIN
                    mm = self.mm_state
                    safe = home
                    if hp_pct >= 35 and mm is not None and mm.pos is not None:
                        own = config.BLUE_TOWERS if self.side == "ORDER" else config.RED_TOWERS
                        tag = "T1" if self.side == "ORDER" else "T2"
                        dead = getattr(self, "_dead_turrets", set())
                        from jev.lanes import LANE_TOWER_BASE
                        alive = [own[b + k] for ln, b in LANE_TOWER_BASE.items() for k, num in enumerate((5, 4, 3))
                                 if b + k < len(own) and f"Turret_{tag}_{LANE_LETTER[ln]}_{num:02d}_A" not in dead]
                        if alive:
                            safe = min(alive, key=lambda t: dist(mm.pos, t))
                    m.go_map(safe, now, attack=False, every=0.8)
                    m.last_action = "retreat: toward " + ("base" if safe == home else "our tower")
                else:
                    v = self.view
                    close = bool(self._enemies_near_on_map(1500)) or (v is not None and bool(v.enemies("champion")))
                    if hp_pct < 40 or (hp_pct < 55 and close):
                        # Low, or half HP with them close: home, not our tower. Holding at the tower at
                        # 19% with a full-HP champion 700 units away was a death in g24 (21:17), and the
                        # "tower" was often a dead one (g22-g26).
                        home = config.BLUE_FOUNTAIN if self.side == "ORDER" else config.RED_FOUNTAIN
                        m.go_map(home, now, attack=False, every=0.8)
                        m.last_action = "retreat: home (low)"
                    else:
                        m.retreat(move_speed, now)
        elif self.intent == "step_back":
            m.step_back(move_speed, now)
        elif self.intent == "defend":
            m.defend(move_speed, now)
        elif self.intent == "group":
            m.group(move_speed, now)
        elif self.intent == "recall":
            if m.recall_started is None and self._shove_first(data, ap, cs, now, hp_pct):
                pass  # the wave into their tower first, then home (it would crash under ours while we are gone)
            elif m.nav.progress > self.lane.center - self.lane.frac(590) or lost > 0 or self._recall_threat(now):
                m.retreat(move_speed, now)  # walk back first, never channel in the middle of the lane
            else:
                m.start_recall(now)
        return p

    def _logline(self, p: Perception, now: float) -> None:
        if not self.logfile or now - self._last_logged < 1.0:
            return
        self._last_logged = now
        mi = self.micro
        if mi is not None and mi.plan_log:
            # The lane planner's picks with their runner-ups (moves at most one a second): what the
            # weights in laneplan.py are tuned from.
            gt = self.state.get("game", {}).get("time")
            with open(f"{self.logfile}.plan.jsonl", "a") as f:
                moved = False
                while mi.plan_log:
                    e = mi.plan_log.popleft()
                    if e["pick"]["kind"] == "hold" or (e["pick"]["kind"] == "move" and moved):
                        continue
                    moved = moved or e["pick"]["kind"] == "move"
                    f.write(json.dumps({"gt": gt, **e}) + "\n")
        me = self.state.get("me", {})
        line = (
            f"{time.strftime('%H:%M:%S')} t={self.state.get('game', {}).get('time')} "
            f"L{me.get('level')} hp={me.get('hp_percent')}% gold={me.get('gold')} {me.get('kda')} cs={me.get('cs')} "
            f"| {p.position} lane={p.lane_progress_pct}% dmg={p.hp_lost_recent_pct:.0f} | intent={self.intent} "
            f"| jev={self.decision.summary() if self.decision else '-'} | act={self.mech.last_action if self.mech else ''} "
            f"keys={'y' if self.ctl.keys_ok() else 'n'} | mm={self._mm_summary()} | {' / '.join(self.fast_summary())} "
            f"| qsig={tuple(round(v) for v in self.view.hud.q_sig) if self.view else '-'} | {self.log_lines[-1] if self.log_lines else ''}\n"
        )
        with open(self.logfile, "a") as f:
            f.write(line)

    def _mm_summary(self) -> str:
        mm = self.mm_state
        if mm is None:
            return "-"
        pos = f"{int(mm.pos[0])},{int(mm.pos[1])}" if mm.pos else "?"
        return f"pos={pos} ec={len(mm.enemy_champions)} em={len(mm.enemy_minions)} am={len(mm.ally_minions)} {mm.ms:.0f}ms"

    def _income_contact(self, gold: float, cs: int, now: float) -> bool:
        """True when gold came in faster than passive income over the last few seconds, or CS
        moved: the champion is killing minions, so the wave is here."""
        self._gold_hist.append((now, gold, cs))
        while self._gold_hist and now - self._gold_hist[0][0] > 6.0:
            self._gold_hist.popleft()
        t0, g0, cs0 = self._gold_hist[0]
        dt = now - t0
        if dt < 3.0:
            return False
        passive = 2.1 * dt + 4.0
        return (gold - g0) > passive + 12 or cs > cs0

    def _items_now(self) -> list[str]:
        """Item names, repeated by stack count (a second potion changes the list, not just the set)."""
        d = self.riot.all_game_data() or {}
        me = find_me(d) or {}
        return [i.get("displayName", "") for i in me.get("items", []) for _ in range(max(1, int(i.get("count", 1) or 1)))]

    def _shop_if_possible(self, gold: float) -> None:
        """Buy toward the build plan's target: the item if the gold covers what is left of its
        recipe, else its most valuable affordable components. Re-planned against current gold."""
        if self.mech is None:
            return
        self._shopping = True
        try:
            self._shop_plan_and_buy(gold)
        finally:
            self._shopping = False
            self._shop_open_since = None

    def _shop_plan_and_buy(self, gold: float) -> None:
        names: list[str] = []
        if self.shop_brain is not None:
            # Re-plan right before buying: a plan made before the last purchase names items we now own.
            try:
                fresh = self.riot.all_game_data() or self.data or {}
                self.build = self.shop_brain.decide(fresh, self.side)
                gold = float((fresh.get("activePlayer") or {}).get("currentGold", gold))
            except Exception as e:  # noqa: BLE001
                self.log_lines.append(f"build error: {e}")
        def have(it) -> bool:
            # An owned upgrade counts: Berserker's Greaves turns into Gunmetal Greaves and the
            # fallback kept trying to buy the boots again (g15).
            cat, owned = self.shop_brain.catalog, self._items_now()
            seen, todo = set(), [it]
            while todo:
                x = todo.pop()
                if x.name in owned:
                    return True
                for i in x.into_ids:
                    if i not in seen and i in cat.items:
                        seen.add(i)
                        todo.append(cat.items[i])
            return False

        if self.build is not None and self.shop_brain is not None:
            cat = self.shop_brain.catalog
            target = cat.get(self.build.target)
            # (A stale plan's target may be owned already: g35 bought Infinity Edge with the API out of
            # credits, then tried to buy it again on every visit instead of the next core item.)
            if target is not None and not have(target):
                names = [b.name for b in cat.purchases(target, self._items_now(), gold)]
            # (No extra Health Potion: typing its name also matches every item mentioning health,
            # so it failed 16 times in two games; the starting bundle already has one.)
        if not names and self.shop_brain is not None and (self.build is None or time.time() - self.build.ts > 90):
            # No build plan from Jev (API down): the kit's core items in order, components as gold allows.
            cat = self.shop_brain.catalog
            owned = self._items_now()
            game_min = float((self.data or {}).get("gameData", {}).get("gameTime", 0.0)) / 60
            from jev.mechanics import PETS

            has_pet = any(k in o.lower() for o in owned for k in PETS)
            if game_min < 1.5 and not [o for o in owned if o not in ("Stealth Ward", "Oracle Lens")]:
                order = list(self.kit.items.starters[:1])
            elif self.jungle_state is not None and not has_pet and any(k in str(self.kit.items.starters[:1]).lower() for k in PETS):
                # A jungler without his pet gets a fraction of the camps' gold and XP: Lee was level 1
                # at 9:00 after "clearing" three camps (g20). The pet before anything else.
                order = list(self.kit.items.starters[:1])
            else:
                order = [b for b in self.kit.items.boots[:1]] + list(self.kit.items.core)
            for name in order:
                it = cat.get(name)
                if it is None or have(it):
                    continue
                names = [b.name for b in cat.purchases(it, owned, gold)]
                if names and (name, tuple(names)) != getattr(self, "_shop_fb_logged", None):
                    self._shop_fb_logged = (name, tuple(names))
                    self.log_lines.append(f"shop (no Jev): toward {name}")
                break
        if not names:
            first = self.kit.items.starters[0] if self.kit.items.starters else "Doran's Blade"
            names = [first] if gold >= 400 and not self._items_now()[1:] else []
        bought_any = False
        fails = self._shop_fails = getattr(self, "_shop_fails", {})  # reset when we leave the fountain
        names = [n for n in names if fails.get(n, 0) < 2]  # an item that failed twice this visit is skipped
        for item in names:
            if self.mech.shop(item, self._items_now):
                self.log_lines.append(f"shop: bought {item}")
                bought_any = True
            else:
                self.log_lines.append(f"shop: could not buy {item}")
                fails[item] = fails.get(item, 0) + 1
                self._camera_recheck_at = time.time() + 12.0  # typed letters may have hit hotkeys: check once we are out
                break
        # Spend the rest: re-plan with the new inventory and gold, up to two more rounds.
        for _ in range(2):
            if not bought_any or self.shop_brain is None:
                break
            data = self.riot.all_game_data() or {}
            gold_left = float((data.get("activePlayer") or {}).get("currentGold", 0))
            if gold_left < 300:
                break
            try:
                self.build = self.shop_brain.decide(data, self.side)
            except Exception:  # noqa: BLE001
                break
            cat = self.shop_brain.catalog
            target = cat.get(self.build.target)
            more = [b.name for b in cat.purchases(target, self._items_now(), gold_left)] if target else []
            bought_any = False
            for item in more:
                if self.mech.shop(item, self._items_now):
                    self.log_lines.append(f"shop: bought {item}")
                    bought_any = True
                else:
                    self.log_lines.append(f"shop: could not buy {item}")
                    break
