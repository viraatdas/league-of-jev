"""The main loop: poll Riot, track HP, ask Jev once a second, act several times a second."""
from __future__ import annotations

import collections
import json
import math
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
from jev.decisions import DecisionLog
from jev.brain import legal_level_ups
from jev.items import BuildPlan, ShopBrain, enemy_team
from jev.jungle import BIG, FIRST_SPAWN, JungleState, camps
from jev import places as map_places
from jev.kits import FIGHT_MODES, Kit, kit_for
from jev.lanes import LANE_LETTER, Lane, lane_for
from jev.micro import Micro, UnitTracker, build_scene
from jev.minimap import MinimapReader, MinimapState, dist, lane_progress, lane_wave
from jev.riot_api import FixtureRecorder, RiotLiveClient
from jev.screen import Screen
from jev.state import Perception, build_state, find_me
from jev import actions
from jev.tactics import TacticalBrain, TacticInput, menu
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
        """Reads every captured frame: health bars and HUD icons each frame, the minimap at most
        every 33 ms (it only feeds macro positioning). ScreenCaptureKit pushes frames as the
        display refreshes; mss is the fallback. One frame feeds all readers, so they agree."""
        from jev.capture import open_capture

        cap = open_capture(self.screen.px_w, self.screen.px_h, prefer=self.capture_backend, fps=self.capture_fps)
        self.capture_name = cap.name
        self.log_lines.append(f"capture: {cap.name}")
        x0, y0, side = config.GEOMETRY.minimap
        seq, n, t_rate, last_mm = 0, 0, time.time(), 0.0
        read_ms: collections.deque[float] = collections.deque(maxlen=120)
        try:
            while not self._stop.is_set():
                f = cap.wait(seq, 0.2)
                if f is None:
                    continue
                seq = f.seq
                if self.paused:
                    continue
                try:
                    t0 = time.perf_counter()
                    frame = f.img
                    if self.mm is not None and f.ts - last_mm >= 0.033:
                        last_mm = f.ts
                        st = self.mm.read(frame[y0:y0 + side, x0:x0 + side])
                        st.ts = f.ts
                        self.mm_state = st
                    if self.vision is not None:
                        v = self.vision.read(frame)
                        v.ts = f.ts  # latency is measured from when the frame was captured
                        self.view = v
                    if f.ts - getattr(self, "_dialog_checked", 0.0) >= 1.5:
                        self._dialog_checked = f.ts
                        self._dialog_ok = self._find_dialog_ok(frame)
                    read_ms.append((time.perf_counter() - t0) * 1000)
                    self.perceive_ms = sorted(read_ms)[len(read_ms) // 2]
                    self.frame_age_ms = (time.time() - f.ts) * 1000
                    self._wake_actor.set()
                    if self.save_frames_s:
                        # Every N seconds, and 4 per second while an enemy champion is on screen
                        # (fights are what reviews look at). JPEG keeps a night of games small.
                        v = self.view
                        fighting = v is not None and (bool(v.enemies("champion")) or bool(v.enemies("monster")))
                        gap = min(self.save_frames_s, 0.25) if fighting else self.save_frames_s
                        if f.ts - getattr(self, "_last_saved", 0.0) >= gap:
                            import cv2
                            from pathlib import Path

                            self._last_saved = f.ts
                            d = Path(self.frames_dir)
                            d.mkdir(parents=True, exist_ok=True)
                            gt = float(((self.data or {}).get("gameData") or {}).get("gameTime", 0.0))
                            name = f"{time.strftime('%H%M%S')}{int(f.ts * 1000) % 1000:03d}_t{int(gt // 60):02d}{int(gt % 60):02d}.jpg"
                            cv2.imwrite(str(d / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                except Exception as e:  # noqa: BLE001
                    self.log_lines.append(f"perception error: {e}")
                    time.sleep(0.05)
                n += 1
                if time.time() - t_rate >= 2.0:
                    self.perceive_fps = n / (time.time() - t_rate)
                    n, t_rate = 0, time.time()
        finally:
            cap.stop()

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
        st = build_state(data, perception, self.role)
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

    def _micro_step(self, data: dict, ap: dict, stats: dict, now: float, standing: bool = True,
                    camp_pt: tuple[float, float] | None = None, camp_big: bool = False) -> bool:
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
        minions = self.min_tracker.update(raw_minions, now)
        champs = self.champ_tracker.update(view.enemies("champion"), now)
        if not minions and not champs and not (self.kit.support and view.allies("champion")):
            self.scene = None
            return False
        ad = float(stats.get("attackDamage", 60.0))
        aspd = float(stats.get("attackSpeed", 0.7))
        q_rank = int(ap.get("abilities", {}).get("Q", {}).get("abilityLevel", 0))
        game_s = float((data.get("gameData") or {}).get("gameTime", 0.0))
        sc = build_scene(view, minions, champs, ad, q_rank, game_s, now, config.GEOMETRY.champion_px,
                         aspd=aspd, move_speed=float(stats.get("moveSpeed", 345.0)))
        if kit.support:
            sc.killable_auto = []  # supports leave last hits to the carry
        self.scene = sc
        mi.fwd = self.lane.screen_dir(self.mech.nav.progress) if self.mech else mi.fwd
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
        mi.summoners, mi.hp_pct = inp.summoners, inp.hp_pct
        self._fight_triggers(sc, mi, now)
        if not mi._can_order(now):
            return True
        if camp_pt is not None and camp_big and self._smite_reflex(ctx, inp, now):
            mi.reacted("reflex", view.ts)
            return True
        if self._escape_reflex(ctx, inp, now):
            mi.reacted("reflex", view.ts)
            return True
        if kit.reflex(mi, sc, now, plan):
            mi.reacted("reflex", view.ts)
            return True
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
            return False
        before = mi.orders
        ok = kit.step(mi, sc, now, aspd, mi.mode, self.intent == "push_tower")
        if mi.orders > before and mi.last_action.startswith("last hit"):
            mi.reacted("lasthit", view.ts)
        return ok

    def _fight_triggers(self, sc, mi, now: float) -> None:
        """Code-level entries and exits around Jev's fight modes: a kill window (enemy champion
        low and close, me healthy) goes all in at once; the strategy head's all_in intent does
        too; a fight I am clearly losing backs off. Supports keep Jev's choice."""
        if self.kit.support:
            return
        ch, d = sc.champ, sc.champ_dist or 9e9
        if ch is not None and not self._champ_on_minimap(now):
            # A champion-sized bar with no enemy champion icon near us on the minimap is a monster
            # (the dragon read as a champion at 8% and got an all-in and an ignite, game 3).
            if mi.mode in FIGHT_MODES:
                mi.set_mode("farm", now)
                self.log_lines.append("fight: no enemy champion on the minimap near me, dropping the fight")
            return
        if mi.mode in FIGHT_MODES:
            if ch is not None and mi.hp_pct < 25 and ch.unit.hp > mi.hp_pct / 100 + 0.15:
                mi.set_mode("back_off", now)
                self.log_lines.append(f"fight: losing ({mi.hp_pct:.0f}% vs {ch.unit.hp * 100:.0f}%), backing off")
            return
        if ch is None or getattr(self, "_near_enemy_tower", False):
            return
        if ch.unit.hp < 0.25 and d < 650 and mi.hp_pct > 35:
            mi.set_mode("all_in", now)
            self.log_lines.append(f"fight: kill window ({ch.unit.hp * 100:.0f}% at {d:.0f}u), all in")
            return
        dd = self.decision
        if (dd is not None and dd.intent == "all_in" and dd.intent_confidence >= 0.5 and d < 900 and mi.hp_pct > 40
                and now - dd.ts < 2.0 and mi.mode != "back_off"):
            mi.set_mode("all_in", now)
            self.log_lines.append(f"fight: strategy says all in (p={dd.intent_confidence:.2f})")

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
        for t, kind, hp in mi.lh_pending:
            if now - t < 1.1:
                keep.append((t, kind, hp))
                continue
            before = [g for tt, g in gh if tt <= t]
            after = [g for tt, g in gh if t < tt <= t + 1.1]
            ok = bool(before and after) and max(after) - before[-1] >= 12 + 2.1 * 1.1
            key = f"{kind} {int(hp * 100) // 5 * 5}%"
            stats[key][0] += 1
            stats[key][1] += int(ok)
            stats[kind][0] += 1
            stats[kind][1] += int(ok)
        mi.lh_pending = keep
        if now - getattr(self, "_lh_logged", 0.0) > 60 and stats:
            self._lh_logged = now
            parts = [f"{k} {v[1]}/{v[0]}" for k, v in sorted(stats.items())]
            self.log_lines.append("lasthits paid: " + ", ".join(parts))

    def _champ_on_minimap(self, now: float, radius: float = 1800.0) -> bool:
        """An enemy champion icon within `radius` of us on a fresh minimap read (True when the
        minimap is unavailable, so vision alone decides)."""
        mm = self.mm_state
        if mm is None or mm.pos is None or now - mm.ts > 1.0:
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
        big = [t for t in sc.minions if t.unit.kind == "monster"] or sc.minions
        tgt = min(big, key=lambda t: t.unit.hp)
        if tgt.unit.hp > 0.22 or sc.dist(tgt) > 550:
            return False
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
            return
        if js.arrived_at is None or js.arrived_at < FIRST_SPAWN:
            js.arrived_at = max(gt, FIRST_SPAWN)
        sc_before = len(self.min_tracker.tracks)
        acted = self._micro_step(data, ap, stats, now, standing=True, camp_pt=pt, camp_big=big)
        if self.scene is not None and self.scene.minions:
            js.last_seen_monster = gt
            return
        # Cleared: monsters were seen here and none for 3 s; or none at all 20 s after arriving
        # (someone else took the camp). "None seen for 3 s" alone marked red cleared before it spawned.
        seen_here = js.last_seen_monster >= js.arrived_at
        if (seen_here and gt - js.last_seen_monster > 3.0) or (not seen_here and gt - js.arrived_at > 20.0):
            js.mark_cleared(js.current, gt)
            self.log_lines.append(f"jungle: cleared {list(js.cleared)[-1]} at {int(gt)}s")
            return
        if not acted:
            m.go_map(pt, now, attack=True, every=1.2)  # step onto the camp so it aggroes
            m.last_action = f"jungle: pulling {js.current}"

    def _escape_reflex(self, ctx, inp, now: float) -> bool:
        """Burst incoming (a quarter of HP gone in the damage window, under 45% HP, an enemy
        champion within 700 units): Flash toward our tower, else a defensive summoner, else a
        potion. Frame-level, no Jev wait; once every 12 s."""
        sc = ctx.sc
        hp, lost = getattr(self, "_hp_now", 100.0), getattr(self, "_hp_lost", 0.0)
        if not (lost >= 25 and hp < 45 and sc.champ is not None and (sc.champ_dist or 9e9) < 700):
            return False
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
            if name != self.lane.name:
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

    def _switch_lane(self, name: str) -> None:
        self.lane = Lane(name, self.side)
        if self.mech is not None:
            self.mech.lane = self.lane
            self.mech.nav.lane_units = self.lane.L
        self.dead_enemy_mid_towers = set()
        self.log_lines.append(f"lane -> {name}")

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
        # Kit from the champion actually in the game; role from the assigned position (empty in
        # custom games, where the kit's default role applies); lane from the role.
        role = self.role_override or str(me.get("position") or "")
        self.kit = kit_for(self.champion_override or me.get("championName", "Yasuo"), role)
        self.role = self.kit.role
        self.lane = Lane(lane_for(self.kit.role, "bot" if self.kit.support else "mid"), side)
        self.mech = Mechanics(self.ctl, self.screen, self.kb, side, lane=self.lane, skill_order=self.kit.skill_order)
        self.micro = Micro(self.ctl, self.screen, self.kb, side)
        self.jungle_state = JungleState(side) if getattr(self.kit, "jungle", False) else None
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
                self.tactics = TacticalBrain(max_hz=self.tactic_hz, explore=self.explore)
                self.tactics.on_answer = self._wake_actor.set
                threading.Thread(target=self.tactics.run, daemon=True).start()
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
                    self._pause_guard(data, t0)
                    self._dismiss_dialog(t0)
                    self._lasthit_check(float((data.get("activePlayer") or {}).get("currentGold", 0.0)), t0)
                    perception = self._tick(data, t0)
                    self.state = self._full_state(data, perception)
                    self.dlog.resolve(self._metrics())
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
                for i, t in enumerate(enemy_towers):
                    if i in lane_ids and lane_ids[i] in self.dead_enemy_mid_towers:
                        continue
                    if dist(mm.pos, t) < config.TOWER_RANGE:
                        p.near_enemy_tower = True
                        break
                self._near_enemy_tower = p.near_enemy_tower
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
            if e.get("EventName") == "TurretKilled" and f"Turret_{enemy_tag}_{LANE_LETTER[self.lane.name]}_" in tk:
                try:
                    self.dead_enemy_mid_towers.add(int(tk.split("_")[3]))
                except (IndexError, ValueError):
                    pass

        # Level-ups: Jev's level_up answer when it arrived after this level was reached (the brain
        # is woken on every level change), otherwise the kit's order after a short wait.
        levels = {k: ap.get("abilities", {}).get(k, {}).get("abilityLevel", 0) for k in ("Q", "W", "E", "R")}
        level = int(ap.get("level", 1))
        if level != self._level_seen:
            self._level_seen, self._level_changed_at = level, now
        if sum(levels.values()) < level and now - self.guards.last_level_t > 1.0:
            d = self.decision
            legal = legal_level_ups(level, levels)
            if d is not None and d.level_up in legal and d.ts >= self._level_changed_at:
                self.guards.last_level_t = now
                m.level_ability(d.level_up)
                self.log_lines.append(f"level {d.level_up} (Jev)")
            elif now - self._level_changed_at > 1.5:
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

        # Standing in our fountain with gold to spend (by the minimap, whatever the phase says):
        # shop once per visit. Covers restarts mid-game, respawns and recalls alike.
        mmp = self.mm_state.pos if self.mm_state is not None else None
        fountain = config.BLUE_FOUNTAIN if self.side == "ORDER" else config.RED_FOUNTAIN
        if mmp is not None:
            d_f = math.dist(mmp, fountain)
            if d_f > 3000:
                self._fountain_shopped = False
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
            if gold >= 50 and self._at_fountain() is not False:
                self._shop_if_possible(gold)
            self._base_shop_done = True
            del self._base_since

        state = self.state or build_state(data, p, self.role)
        self.intent = choose_intent(self.decision, state, p, now, self.guards)
        d0 = self.decision
        if self.intent in ("go_to", "group") and d0 is not None and d0.intent_probabilities.get(self.intent, 0.0) < 0.35:
            self.intent = "farm"  # a low-confidence roam costs a laner CS and exposes them; keep laning
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
            if m.nav.progress > self.lane.center - self.lane.frac(590) or lost > 0:
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
        """Item names, repeated by stack count (a second potion changes the list, not just the set)."""
        d = self.riot.all_game_data() or {}
        me = find_me(d) or {}
        return [i.get("displayName", "") for i in me.get("items", []) for _ in range(max(1, int(i.get("count", 1) or 1)))]

    def _shop_if_possible(self, gold: float) -> None:
        """Buy toward the build plan's target: the item if the gold covers what is left of its
        recipe, else its most valuable affordable components. Re-planned against current gold."""
        if self.mech is None:
            return
        names: list[str] = []
        if self.shop_brain is not None:
            # Re-plan right before buying: a plan made before the last purchase names items we now own.
            try:
                fresh = self.riot.all_game_data() or self.data or {}
                self.build = self.shop_brain.decide(fresh, self.side)
                gold = float((fresh.get("activePlayer") or {}).get("currentGold", gold))
            except Exception as e:  # noqa: BLE001
                self.log_lines.append(f"build error: {e}")
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
            first = self.kit.items.starters[0] if self.kit.items.starters else "Doran's Blade"
            names = [first] if gold >= 400 else []
        bought_any = False
        for item in names:
            if self.mech.shop(item, self._items_now):
                self.log_lines.append(f"shop: bought {item}")
                bought_any = True
            else:
                self.log_lines.append(f"shop: could not buy {item}")
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
