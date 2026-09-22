"""The main loop: poll Riot, track HP, ask Jev once a second, act several times a second."""
from __future__ import annotations

import collections
import json
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from jev import config, keybinds
from jev.brain import Brain, Decision
from jev.control import Controller, activate_game
from jev.mechanics import Mechanics
from jev.minimap import MinimapReader, MinimapState, dist, lane_progress, lane_wave
from jev.riot_api import FixtureRecorder, RiotLiveClient
from jev.screen import Screen
from jev.state import Perception, build_state, find_me

console = Console()


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
    if p.hp_lost_recent_pct >= config.TIMING.heavy_damage_pct:
        guard.retreat_until = now + 5.0  # tower-sized chunks: step out before the next shot
    if p.near_enemy_tower and (d is None or d.intent != "push_tower"):
        guard.step_back_until = now + 2.5
    if p.nearest_enemy_champion_units is not None and p.nearest_enemy_champion_units < 600 and me["hp_percent"] < 30:
        guard.retreat_until = now + 5.0
    if now < guard.retreat_until:
        return "retreat"
    if now < guard.step_back_until:
        return "step_back"
    if d is None:
        return "farm"
    if d.danger >= 2.5:
        return "retreat"
    if d.intent == "recall" or (d.should_recall >= 0.8 and since_dmg > 6):
        return "recall"
    if d.intent in ("trade", "all_in"):
        return "trade"
    return d.intent


class Guards:
    """Timers that keep code-level safety rules sticky across Jev ticks."""

    def __init__(self) -> None:
        self.retreat_until = 0.0
        self.step_back_until = 0.0
        self.left_base_at = 0.0
        self.last_level_t = 0.0
        self.resync_until = 0.0


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
    def __init__(self, dry_run: bool = False, quick_cast: bool | None = None, role: str = "MIDDLE", logfile=None, keep_front: bool = True, forever: bool = False) -> None:
        self.dry_run = dry_run
        self.forever = forever
        self.keep_front = keep_front
        self._last_activate = 0.0
        self.logfile = logfile
        self._last_logged = 0.0
        self.role = role
        self.log_lines: collections.deque[str] = collections.deque(maxlen=8)
        self.ctl = Controller(dry_run=dry_run, log=self._log_action)
        self.screen = Screen()
        self.kb = keybinds.load()
        self.riot = RiotLiveClient()
        self.brain = Brain()
        self.decision: Decision | None = None
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
        self.mm_state: MinimapState | None = None
        self.side = "ORDER"
        self.dead_enemy_mid_towers: set[int] = set()

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
                    self.decision = self.brain.decide(st)
                    self.recorder.write(st, {"decision": self.decision.summary()})
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"brain error: {e}")
            self._wake.wait(timeout=max(0.05, period - (time.time() - t0)))
            self._wake.clear()

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
        self.mm_state = None
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
        self.mech = Mechanics(self.ctl, self.screen, self.kb, side)
        console.print(f"game found, side {side}, champion {me.get('championName')}")
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
        tick = 1 / config.TIMING.tick_hz
        perception = Perception()
        try:
            with Live(self._table(perception), refresh_per_second=4, console=console) as live:
                while True:
                    t0 = time.time()
                    data = self.riot.all_game_data()
                    if data is None:
                        break
                    if not self.dry_run and not self.ctl.keys_ok():
                        if self.keep_front and t0 - self._last_activate > 3.0:
                            self._last_activate = t0
                            activate_game()
                            time.sleep(0.4)
                        if not self.ctl.keys_ok():
                            self.paused = True
                            perception = Perception(position="paused: game window not active")
                            self.state = build_state(data, perception, self.role)
                            live.update(self._table(perception))
                            self._logline(perception, t0)
                            time.sleep(0.5)
                            continue
                    if self.paused:
                        self.paused = False
                        if self.mech:
                            self.mech._last_move = 0.0
                    perception = self._tick(data, t0)
                    self.state = build_state(data, perception, self.role)
                    live.update(self._table(perception))
                    self._logline(perception, t0)
                    time.sleep(max(0.0, tick - (time.time() - t0)))
        finally:
            self._stop.set()
        console.print("game over")

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
            try:
                mm = self.mm.read()
            except Exception:  # noqa: BLE001
                mm = None
            self.mm_state = mm
            if mm is not None and mm.pos is not None:
                prog, lat = lane_progress(mm.pos, self.side)
                m.nav.progress = prog
                m.nav._last_t = now
                p.lane_progress_pct = m.nav.pct
                wave = lane_wave(mm, self.side)
                p.enemy_champions_on_minimap = len(mm.enemy_champions)
                p.enemy_minions_in_lane, p.ally_minions_in_lane = wave.enemy_count, wave.ally_count
                if mm.enemy_champions:
                    p.nearest_enemy_champion_units = min(dist(mm.pos, e) for e in mm.enemy_champions)
                if wave.enemy_front is not None:
                    f = wave.enemy_front
                    p.wave_position = ("under my tower" if f < 0.44 else "on my side of the lane" if f < 0.49
                                       else "at the middle" if f < 0.52 else "on their side" if f < 0.56 else "under their tower")
                # Tower safety: enemy mid towers still standing.
                enemy_towers = config.RED_TOWERS if self.side == "ORDER" else config.BLUE_TOWERS
                mid_ids = {3: 5, 4: 4, 5: 3}  # index in list -> mid outer/inner/inhib tower number
                for i, t in enumerate(enemy_towers):
                    if i in mid_ids and mid_ids[i] in self.dead_enemy_mid_towers:
                        continue
                    if dist(mm.pos, t) < config.TOWER_RANGE:
                        p.near_enemy_tower = True
                        break
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
        # Track destroyed enemy mid towers from the event log.
        enemy_tag = "T2" if self.side == "ORDER" else "T1"
        for e in (data.get("events") or {}).get("Events", []):
            tk = str(e.get("TurretKilled", ""))
            if e.get("EventName") == "TurretKilled" and f"Turret_{enemy_tag}_C_" in tk:
                try:
                    self.dead_enemy_mid_towers.add(int(tk.split("_")[3]))
                except (IndexError, ValueError):
                    pass

        # Level-ups never need Jev.
        levels = {k: ap.get("abilities", {}).get(k, {}).get("abilityLevel", 0) for k in ("Q", "W", "E", "R")}
        if sum(levels.values()) < int(ap.get("level", 1)) and now - self.guards.last_level_t > 1.0:
            self.guards.last_level_t = now
            m.level_up(levels)

        if me.get("isDead"):
            if self.phase != "dead":
                m.cancel_recall()
            self.phase = "dead"
            m.nav.reset_to_base()
            self.intent = "dead"
            self._base_shop_done = False
            return p
        if self.phase == "dead":
            self.phase = "base"
            self._camera_checked = False

        if not getattr(self, "_camera_checked", False) and self.mm is not None and self.ctl.keys_ok():
            self._camera_checked = True
            note = m.ensure_camera_locked(self.mm)
            self.log_lines.append(note)

        # Shop once per visit to base: at game start, after respawn, after a recall.
        if self.phase == "base" and not self._base_shop_done:
            gold = float(ap.get("currentGold", 0))
            if self.decision is None and now - self.guards.left_base_at < 3.0 and gold >= 450:
                return p  # give Jev a moment to name the item
            if gold >= 450:
                self._shop_if_possible()
            self._base_shop_done = True

        state = self.state or build_state(data, p, self.role)
        self.intent = choose_intent(self.decision, state, p, now, self.guards)
        if self.intent == "recall" and now - self.guards.left_base_at < config.TIMING.no_recall_after_base_s:
            self.intent = "farm"

        if m.recall_started is not None:
            if lost > 1.0:
                m.cancel_recall()
                self.intent = "retreat"
            elif m.recall_done(now):
                m.cancel_recall()
                m.nav.reset_to_base()
                self.phase = "base"
                self._base_shop_done = False
                self.intent = "farm"
            else:
                return p

        if self.phase == "base":
            m.go_lane(move_speed, now)
            if m.nav.progress >= config.OWN_TOWER:
                self.phase = "lane"
                self.guards.left_base_at = now
            p.position = "traveling"
            return p

        p.position = "lane"
        if now < self.guards.resync_until and (self.mm_state is None or self.mm_state.pos is None):
            m.retreat(move_speed, now)  # walking to own tower to re-base position
            self.intent = "resync"
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
            m.retreat(move_speed, now)
        elif self.intent == "step_back":
            m.step_back(move_speed, now)
        elif self.intent == "defend":
            m.defend(move_speed, now)
        elif self.intent == "group":
            m.group(move_speed, now)
        elif self.intent == "recall":
            if m.nav.progress > config.LANE_CENTER - 0.03 or lost > 0:
                m.retreat(move_speed, now)  # walk back first, never channel in the middle of the lane
            else:
                m.start_recall(now)
        return p

    def _logline(self, p: Perception, now: float) -> None:
        if not self.logfile or now - self._last_logged < 1.0:
            return
        self._last_logged = now
        me = self.state.get("me", {})
        line = (
            f"{time.strftime('%H:%M:%S')} t={self.state.get('game', {}).get('time')} "
            f"L{me.get('level')} hp={me.get('hp_percent')}% gold={me.get('gold')} {me.get('kda')} cs={me.get('cs')} "
            f"| {p.position} lane={p.lane_progress_pct}% dmg={p.hp_lost_recent_pct:.0f} | intent={self.intent} "
            f"| jev={self.decision.summary() if self.decision else '-'} | act={self.mech.last_action if self.mech else ''} "
            f"keys={'y' if self.ctl.keys_ok() else 'n'} | mm={self._mm_summary()} | {self.log_lines[-1] if self.log_lines else ''}\n"
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
        d = self.riot.all_game_data() or {}
        me = find_me(d) or {}
        return [i.get("displayName", "") for i in me.get("items", [])]

    def _shop_if_possible(self) -> None:
        d = self.decision
        item = d.next_item if d and d.next_item else "Doran's Blade"
        if self.mech:
            if self.mech.shop(item, self._items_now):
                self.log_lines.append(f"shop: bought {item}")
            else:
                self.log_lines.append(f"shop: could not buy {item}")
