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
from jev.items import BuildPlan, ShopBrain, enemy_team
from jev.micro import Micro, UnitTracker, build_scene
from jev.minimap import MinimapReader, MinimapState, dist, lane_progress, lane_wave
from jev.riot_api import FixtureRecorder, RiotLiveClient
from jev.screen import Screen
from jev.state import Perception, build_state, find_me
from jev.tactics import TacticalBrain
from jev.vision import View, VisionReader

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
    def __init__(self, dry_run: bool = False, quick_cast: bool | None = None, role: str = "MIDDLE", logfile=None, keep_front: bool = True, forever: bool = False, tactic_hz: float = 8.0) -> None:
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
        # Fast path: perception thread (screen), API thread, tactical Jev thread, 30 Hz actor.
        self.vision: VisionReader | None = VisionReader() if (config.GEOMETRY.minimap and not dry_run) else None
        self.view: View | None = None
        self.perceive_fps = 0.0
        self.min_tracker = UnitTracker()
        self.champ_tracker = UnitTracker(max_jump_px=90)
        self.tactic_hz = tactic_hz
        self.tactics: TacticalBrain | None = None
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
                    self.decision = self.brain.decide(st)
                    self.recorder.write(st, {"decision": self.decision.summary()})
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"brain error: {e}")
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
        """One full-screen grab per frame (the fixed capture cost dominates), sliced for the
        minimap and read for health bars and HUD icons. Runs as fast as capture allows."""
        import mss
        import numpy as np

        sct = mss.mss()
        mon = sct.monitors[1]
        x0, y0, side = config.GEOMETRY.minimap
        n, t_rate = 0, time.time()
        while not self._stop.is_set():
            if self.paused:
                time.sleep(0.1)
                continue
            try:
                frame = np.array(sct.grab(mon))[:, :, :3]
                if self.mm is not None:
                    self.mm_state = self.mm.read(np.ascontiguousarray(frame[y0:y0 + side, x0:x0 + side]))
                if self.vision is not None:
                    self.view = self.vision.read(frame)
            except Exception as e:  # noqa: BLE001
                self.log_lines.append(f"perception error: {e}")
                time.sleep(0.2)
            n += 1
            if time.time() - t_rate >= 2.0:
                self.perceive_fps = n / (time.time() - t_rate)
                n, t_rate = 0, time.time()

    def _api_loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            d = self.riot.all_game_data()
            self.api_ms = (time.time() - t0) * 1000
            if d is None:
                fails += 1
                if fails >= 4:
                    self._api_dead = True
            else:
                fails = 0
                self.data = d
            time.sleep(max(0.0, 0.1 - (time.time() - t0)))

    def _full_state(self, data: dict, perception: Perception) -> dict:
        st = build_state(data, perception, self.role)
        if self.shop_brain is not None:
            st["enemy_lineup"] = [{k: e[k] for k in ("champion", "class", "damage", "level", "kda")}
                                  for e in enemy_team(data, self.shop_brain.catalog, self.side)]
        shopping = self._shopping_state(data)
        if shopping:
            st["shopping"] = shopping
        return st

    def fast_summary(self) -> list[str]:
        """Overlay lines for the fast path."""
        out = []
        t = self.tactics.tactic if self.tactics else None
        rate = self.tactics.rate() if self.tactics else 0.0
        if t is not None:
            probs = sorted(t.probabilities.items(), key=lambda kv: -kv[1])[:4]
            out.append(f"tactic -> {t.action.upper()} p={t.confidence:.2f} ({t.latency_ms:.0f} ms, {rate:.1f}/s)")
            out.append("  " + "  ".join(f"{k} {v:.2f}" for k, v in probs))
        sc = self.scene
        if sc is not None:
            ch = f"champ {int(sc.champ.unit.hp * 100)}% @{int(sc.champ_dist or 0)}u" if sc.champ else "no champ"
            rdy = "".join(k for k in "QWER" if sc.ready.get(k))
            out.append(f"screen: {len(sc.minions)} minions ({len(sc.killable_auto)} killable), {sc.allies} ally, {ch}, ready {rdy or '-'}"
                       + (" Q3" if self.micro and self.micro.q.q3(time.time()) else ""))
        b = self.build
        if b is not None:
            needs = " ".join(f"{k.replace('need_', '')[:7]} {v:.2f}" for k, v in b.needs.items())
            out.append(f"build -> {b.target} p={b.confidence:.2f} | buy now: {', '.join(b.buy_now) or '-'}")
            out.append(f"  needs: {needs}")
        out.append(f"APM {self.ctl.apm()}   vision {self.perceive_fps:.0f} fps   api {self.api_ms:.0f} ms   micro: {self.micro.last_action if self.micro else '-'}")
        return out

    def _micro_step(self, data: dict, ap: dict, stats: dict, now: float) -> bool:
        """Screen-level play while units are on screen: Jev's tactical choice, reflexes, and
        last-hit farming. Returns False when there is nothing on screen (macro moves instead)."""
        view, mi = self.view, self.micro
        if view is None or mi is None or now - view.ts > 0.3:
            return False
        minions = self.min_tracker.update(view.enemies("minion"), now)
        champs = self.champ_tracker.update(view.enemies("champion"), now)
        if not minions and not champs:
            self.scene = None
            return False
        ad = float(stats.get("attackDamage", 60.0))
        aspd = float(stats.get("attackSpeed", 0.7))
        q_rank = int(ap.get("abilities", {}).get("Q", {}).get("abilityLevel", 0))
        game_s = float((data.get("gameData") or {}).get("gameTime", 0.0))
        sc = build_scene(view, minions, champs, ad, q_rank, game_s, now, config.GEOMETRY.champion_px)
        self.scene = sc
        q3 = mi.q.q3(now)
        d = self.decision
        plan = {
            "intent": self.intent,
            "aggression_0_to_2": round(d.aggression, 1) if d else 1.0,
            "danger": round(d.danger, 1) if d else 0.0,
            "fight_favorable": round(d.fight_favorable, 2) if d else 0.5,
        }
        if self.tactics is not None:
            self.tactics.publish(sc, q3, self.state, plan, view.ts)
        if not mi._can_order(now):
            return True
        if mi.reflexes(sc, now, d is None or d.fight_favorable >= 0.45):
            return True
        t = self.tactics.tactic if self.tactics else None
        if t is not None and t.seq > mi.last_seq and t.age(now) < config.FAST.tactic_stale_s:
            mi.last_seq = t.seq
            if t.action in ("farm", "push", "back_off"):
                mi.mode = t.action
            elif mi.execute(t.action, sc, now, aspd, t.dash_target):
                return True
        if mi.mode == "back_off" and sc.champ is not None:
            mi.back_off(sc, now)
            return True
        return mi.farm_step(sc, now, aspd, push=(mi.mode == "push" or self.intent == "push_tower"))

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
        self.view = None
        self.scene = None
        self.min_tracker = UnitTracker()
        self.champ_tracker = UnitTracker(max_jump_px=90)
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
        self.micro = Micro(self.ctl, self.screen, self.kb, side)
        self.data = data
        self._api_dead = False
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
        threading.Thread(target=self._api_loop, daemon=True).start()
        threading.Thread(target=self._build_loop, daemon=True).start()
        if not self.dry_run:
            threading.Thread(target=self._perceive_loop, daemon=True).start()
            if self.tactic_hz > 0:
                self.tactics = TacticalBrain(max_hz=self.tactic_hz)
                threading.Thread(target=self.tactics.run, daemon=True).start()
        tick = 1 / (config.FAST.act_hz if not self.dry_run else config.TIMING.tick_hz)
        perception = Perception()
        try:
            with Live(self._table(perception), refresh_per_second=4, console=console) as live:
                while True:
                    t0 = time.time()
                    data = self.data if not self.dry_run else self.riot.all_game_data()
                    if data is None or self._api_dead:
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
                    self.state = self._full_state(data, perception)
                    if t0 - self._last_table > 0.25:
                        self._last_table = t0
                        live.update(self._table(perception))
                    self._logline(perception, t0)
                    time.sleep(max(0.0, tick - (time.time() - t0)))
        finally:
            self._stop.set()
            if self.tactics is not None:
                self.tactics.stop()
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
            mm = self.mm_state  # read by the perception thread
            if mm is not None and mm.pos is not None and now - mm.ts < 1.0:
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
                self._build_wake.set()
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
            if not hasattr(self, "_base_since"):
                self._base_since = now
            fresh = self.build is not None and now - self.build.ts < 6.0
            if self.shop_brain is not None and not fresh and now - self._base_since < 3.0:
                self._build_wake.set()
                return p  # give the build head a moment to re-plan with the current gold
            if gold >= 50:
                self._shop_if_possible(gold)
            self._base_shop_done = True
            del self._base_since

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
                self._build_wake.set()
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
        if self.intent in ("farm", "trade", "push_tower", "defend") and self._micro_step(data, ap, cs, now):
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
        d = self.riot.all_game_data() or {}
        me = find_me(d) or {}
        return [i.get("displayName", "") for i in me.get("items", [])]

    def _shop_if_possible(self, gold: float) -> None:
        """Buy toward the build plan's target: the item if the gold covers what is left of its
        recipe, else its most valuable affordable components. Re-planned against current gold."""
        if self.mech is None:
            return
        names: list[str] = []
        if self.build is not None and self.shop_brain is not None:
            cat = self.shop_brain.catalog
            target = cat.get(self.build.target)
            if target is not None:
                names = [b.name for b in cat.purchases(target, self._items_now(), gold)]
            spent = sum(cat.get(n).price for n in names if cat.get(n))
            game_min = float((self.data or {}).get("gameData", {}).get("gameTime", 0.0)) / 60
            potions = sum(1 for i in self._items_now() if "potion" in i.lower())
            if game_min < 12 and potions < 2 and gold - spent >= 50:
                names.append("Health Potion")
        if not names:
            names = ["Doran's Blade"] if gold >= 450 else []
        for item in names:
            if self.mech.shop(item, self._items_now):
                self.log_lines.append(f"shop: bought {item}")
            else:
                self.log_lines.append(f"shop: could not buy {item}")
                break
