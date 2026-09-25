"""Micro: unit tracking, last-hit prediction, and the executor for Jev's tactical choices.

Everything here runs in the fast loop (30 Hz) on the latest screen read. Screen positions
come from health bars (vision.py); the own champion's screen position comes from its own
bar, so none of this depends on the camera lock.
"""
from __future__ import annotations

import collections
import itertools
import math
import random
import time
from dataclasses import dataclass, field

from jev import config
from jev.control import Controller
from jev.keybinds import Keybinds
from jev.screen import Screen
from jev.vision import Unit, View

FAST = config.FAST
VC = config.VISION
_ids = itertools.count(1)


@dataclass
class Track:
    id: int
    unit: Unit
    seen: float
    hist: collections.deque = field(default_factory=lambda: collections.deque(maxlen=24))
    e_marked_until: float = 0.0   # Yasuo E cannot dash through the same unit again for a while
    path: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))  # (t, x, y) screen px
    trail: collections.deque = field(default_factory=lambda: collections.deque(maxlen=50))  # (t, hp) every 0.1 s, 5 s
    drops: collections.deque = field(default_factory=lambda: collections.deque(maxlen=16))  # (t, hp fraction lost) per hit
    hits_expected: list = field(default_factory=list)   # (t, damage) of our own hits on the way
    hp_max_measured: float | None = None                 # max HP from the drop our own known-damage hit made

    def hp_change(self, now: float, seconds: float) -> float | None:
        """HP fraction gained (+) or lost (-) over the last `seconds`, from the slow trail."""
        old = [h for t, h in self.trail if now - t >= seconds - 0.05]
        if not old or not self.trail:
            return None
        return self.trail[-1][1] - old[-1]

    def velocity(self, now: float, window: float = 0.35) -> tuple[float, float]:
        """Screen px per second over the recent window (0, 0 with too little history)."""
        pts = [(t, x, y) for t, x, y in self.path if now - t <= window]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 0.08:
            return 0.0, 0.0
        dt = pts[-1][0] - pts[0][0]
        return (pts[-1][1] - pts[0][1]) / dt, (pts[-1][2] - pts[0][2]) / dt

    def lead(self, now: float, delay_s: float) -> tuple[float, float]:
        """Where the unit will be after `delay_s` if it keeps walking the same way (capped)."""
        vx, vy = self.velocity(now)
        cap = 420 * config.VISION.px_per_unit  # no faster than ~420 units/s
        n = math.hypot(vx, vy)
        if n > cap:
            vx, vy = vx / n * cap, vy / n * cap
        return self.unit.x + vx * delay_s, self.unit.y + vy * delay_s

    def hp_rate(self, now: float, window: float = 0.7) -> float:
        """HP fraction per second over the recent window (negative while taking damage)."""
        pts = [(t, h) for t, h in self.hist if now - t <= window]
        if len(pts) < 3:
            return 0.0
        t0 = pts[0][0]
        xs = [t - t0 for t, _ in pts]
        ys = [h for _, h in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 1e-6:
            return 0.0
        return min(0.0, sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den)

    def predict_linear(self, now: float, lead: float) -> float:
        return self.unit.hp + self.hp_rate(now) * lead

    def predict_steps(self, now: float, lead: float) -> float | None:
        """Minion HP falls in steps, one per hit: the next drops come at the hit rhythm (median
        interval and size of the last 2.5 s of drops). None with fewer than two drops."""
        rec = [(t, a) for t, a in self.drops if now - t <= 2.5]
        if len(rec) < 2:
            return None
        ivals = sorted(b[0] - a[0] for a, b in zip(rec, rec[1:]))
        period = min(2.0, max(0.25, ivals[len(ivals) // 2]))
        size = sorted(a for _, a in rec)[len(rec) // 2]
        nxt = max(now, rec[-1][0] + period)
        n = 0 if now + lead < nxt else 1 + int((now + lead - nxt) / period)
        return self.unit.hp - n * size

    def predict_hp(self, now: float, lead: float) -> float:
        """HP fraction `lead` seconds from now, by whichever forecast has been more accurate in this
        game so far (HP_MODEL scores both against what happened, live)."""
        if HP_MODEL.best() == "steps":
            st = self.predict_steps(now, lead)
            if st is not None:
                return st
        return self.predict_linear(now, lead)


class HpModel:
    """Scores the two HP forecasts against what happened 0.5 s later, during the game, and names the
    better one (exponential average of the absolute error in HP fraction)."""

    HORIZON = 0.5

    def __init__(self) -> None:
        self.err = {"linear": 0.030, "steps": 0.031}   # the linear forecast starts as the incumbent
        self.n = {"linear": 0, "steps": 0}
        self.pending: collections.deque = collections.deque(maxlen=400)  # (due, track, {model: forecast})
        self._last_sample = 0.0

    def best(self) -> str:
        return "steps" if self.n["steps"] >= 30 and self.err["steps"] < self.err["linear"] * 0.95 else "linear"

    def observe(self, tracks: list, now: float) -> None:
        while self.pending and self.pending[0][0] <= now:
            _, tr, preds = self.pending.popleft()
            if now - tr.seen > 0.1:
                continue  # gone (dead or off screen): no clean answer
            for k, v in preds.items():
                self.err[k] += 0.03 * (abs(v - tr.unit.hp) - self.err[k])
                self.n[k] += 1
        if now - self._last_sample < 0.2:
            return
        self._last_sample = now
        for tr in tracks:
            if tr.unit.kind != "minion" or tr.unit.team != "enemy" or tr.unit.hp > 0.6:
                continue
            st = tr.predict_steps(now, self.HORIZON)
            if st is None:
                continue  # score both on the same cases
            self.pending.append((now + self.HORIZON, tr, {"linear": tr.predict_linear(now, self.HORIZON), "steps": st}))

    def summary(self) -> str:
        return f"hp forecast: {self.best()} (err linear {self.err['linear']:.3f} n={self.n['linear']}, steps {self.err['steps']:.3f})"


HP_MODEL = HpModel()


class UnitTracker:
    """Keeps identities across frames by nearest-neighbour matching of screen positions."""

    def __init__(self, max_jump_px: float = 55.0, ttl: float = 0.5) -> None:
        self.tracks: dict[int, Track] = {}
        self.max_jump = max_jump_px
        self.ttl = ttl
        self.dropped: list[Track] = []

    def update(self, units: list[Unit], now: float) -> list[Track]:
        free = dict(self.tracks)
        out: list[Track] = []
        for u in sorted(units, key=lambda u: u.hp):
            best, bd = None, self.max_jump
            for tid, tr in free.items():
                if tr.unit.kind != u.kind:
                    continue
                d = math.hypot(tr.unit.x - u.x, tr.unit.y - u.y)
                if d < bd:
                    best, bd = tid, d
            if best is not None:
                tr = free.pop(best)
                prev = [h for _, h in list(tr.hist)[-2:]]
                if prev and u.hp < min(prev) - 0.015 and u.kind == "minion":
                    drop = min(prev) - u.hp
                    tr.drops.append((now, drop))
                    # Our own hit of known damage landing now: the drop gives the minion's max HP
                    # (melee ~477, caster ~296 early), better than guessing from its place in the wave.
                    for t_hit, dmg in tr.hits_expected:
                        if abs(now - t_hit) <= 0.2 and 150.0 <= dmg / drop <= 2500.0:
                            tr.hp_max_measured = dmg / drop
                            break
                tr.hits_expected = [(t, d) for t, d in tr.hits_expected if now - t < 0.4]
                tr.unit, tr.seen = u, now
            else:
                tr = Track(next(_ids), u, now)
                self.tracks[tr.id] = tr
            tr.hist.append((now, u.hp))
            tr.path.append((now, u.x, u.y))
            if not tr.trail or now - tr.trail[-1][0] >= 0.1:
                tr.trail.append((now, u.hp))
            out.append(tr)
        HP_MODEL.observe(out, now)
        self.dropped = []
        for tid, tr in list(self.tracks.items()):
            if now - tr.seen > self.ttl:
                self.dropped.append(tr)  # died or left the screen (the last-hit audit reads these)
                del self.tracks[tid]
        return out


def minion_max_hp(game_s: float) -> float:
    """Between caster and melee minion HP. Assuming the melee pool made every last hit too late
    (allied minions took them first); the midpoint commits in time, at the cost of an occasional
    early swing on a melee minion."""
    mb, mp = FAST.melee_hp
    cb, cp = FAST.caster_hp
    t = max(0.0, game_s) / 90.0
    return ((mb + mp * t) + (cb + cp * t)) / 2


@dataclass
class Scene:
    """What the tactical layer and the reflexes need, in game units from the own champion."""

    me_xy: tuple[float, float]
    minions: list[Track] = field(default_factory=list)       # enemy minions
    allies: int = 0
    ally_champs: list[Unit] = field(default_factory=list)     # allied champions on screen (the carry, for a support)
    champ: Track | None = None                                # nearest enemy champion
    enemy_champs: int = 0                                     # enemy champions on screen
    champ_dist: float | None = None
    killable_auto: list[Track] = field(default_factory=list)
    killable_q: list[Track] = field(default_factory=list)
    killable_e: list[Track] = field(default_factory=list)     # a dash through it kills it (Yasuo E)
    soon_killable: list[Track] = field(default_factory=list)   # one auto kills it within ~1.2 s: get in range now
    dash_toward: Track | None = None                          # minion to E through toward the champion
    dash_options: list[tuple[Track, float]] = field(default_factory=list)  # (minion, units gained toward champion)
    ready: dict[str, bool] = field(default_factory=dict)
    r_lit: bool = False
    ally_units: list[Unit] = field(default_factory=list)      # allied minions on screen (our wave's front)
    champs: list[Track] = field(default_factory=list)         # every enemy champion on screen
    ad: float = 60.0
    q_dmg: float = 0.0                                        # Q on a minion, with the model margin
    e_dmg: float = 0.0                                        # E on a minion (Yasuo), with the margin
    aspd: float = 0.7
    move_speed: float = 345.0
    lane: dict = field(default_factory=dict)                  # set by the loop: opp_range, tower_px, aggression, ...

    def dist(self, tr: Track) -> float:
        return math.hypot(tr.unit.x - self.me_xy[0], tr.unit.y - self.me_xy[1]) / VC.px_per_unit


def minion_roles(minions: list[Track], me_xy, fwd) -> dict[int, str]:
    """Melee or caster by place in the wave: melee minions walk in front (nearest our side along
    the lane), casters behind. All minion bars are 60 px, so the bar alone cannot tell them apart."""
    if fwd is None or not minions:
        return {}
    along = lambda t: (t.unit.x - me_xy[0]) * fwd[0] + (t.unit.y - me_xy[1]) * fwd[1]
    order = sorted(minions, key=along)
    n_melee = min(3, max(1, len(order) // 2)) if len(order) >= 2 else 0
    return {t.id: ("melee" if i < n_melee else "caster") for i, t in enumerate(order)}


def build_scene(view: View, minions: list[Track], champs: list[Track], ad: float, q_rank: int, game_s: float, now: float, fallback_xy,
                aspd: float = 0.7, move_speed: float = 345.0, fwd=None, e_dmg: float = 0.0) -> Scene:
    me_xy = (view.me.x, view.me.y) if view.me else fallback_xy
    sc = Scene(me_xy=me_xy, minions=minions, allies=len(view.allies("minion")), ally_champs=view.allies("champion"),
               ally_units=view.allies("minion"), ad=ad, e_dmg=e_dmg, aspd=aspd, move_speed=move_speed)
    sc.ready = dict(view.hud.ready)
    sc.r_lit = bool(view.hud.ready.get("R"))
    sc.enemy_champs = len(champs)
    sc.champs = list(champs)
    if champs:
        sc.champ = min(champs, key=sc.dist)
        sc.champ_dist = sc.dist(sc.champ)
    avg_max = minion_max_hp(game_s)
    melee_max = FAST.melee_hp[0] + FAST.melee_hp[1] * max(0.0, game_s) / 90.0
    caster_max = FAST.caster_hp[0] + FAST.caster_hp[1] * max(0.0, game_s) / 90.0
    roles = minion_roles(minions, me_xy, fwd)
    # 12% under the formula: over g15-g17, Q on melee minions paid 80% at 10-25% HP but 51% at 30%
    # and 32% at 35%; the model reaches further than the game does.
    q_dmg = 0.88 * (FAST.q_base[max(0, min(q_rank, 5) - 1)] + FAST.q_ad * ad) if q_rank else 0.0
    sc.q_dmg = q_dmg
    windup = FAST.windup_frac / max(0.3, aspd)
    for tr in minions:
        d = sc.dist(tr)
        tr.role = roles.get(tr.id, "")
        if tr.hp_max_measured is not None:
            # Measured from our own hit: its role follows (nearer the melee or the caster pool).
            tr.role = "melee" if abs(tr.hp_max_measured - melee_max) < abs(tr.hp_max_measured - caster_max) else "caster"
            hp_max = melee_max if tr.role == "melee" else caster_max
        else:
            hp_max = melee_max if tr.role == "melee" else (caster_max if tr.role == "caster" else avg_max)
        tr.hp_max = hp_max
        tr.ally_takes = (tr.unit.hp < 0.12 and sc.allies > 0) or tr.unit.hp < 0.04 or (tr.role == "caster" and tr.unit.hp < 0.12)
        if (tr.unit.hp < 0.12 and sc.allies > 0) or tr.unit.hp < 0.04 or (tr.role == "caster" and tr.unit.hp < 0.12):
            # Nearly dead with allied minions around: they take it before our hit lands (g11: Q at
            # 0-15% paid 8/29, at 20%+ 17/23; autos at 0-10% 3/15). A caster's bar under 12% (7 px)
            # paid 10 of 297 attempts over g15-g17, and any bar under 4% is noise. Melee minions at 5-9% with
            # ours around paid 11% of auto attempts over g22-g26 (15-24%: 54-60%): one allied hit finishes
            # them first, and the wasted auto is on cooldown for the next real last hit.
            continue
        # Forecast the minion's HP at the moment the hit lands: input, the walk into range, the
        # wind-up. With only the input lead, a hit that needed a walk landed after the allied
        # minions had taken the kill ("last hit (3%)" on a dying minion, game 2).
        walk = max(0.0, d - VC.auto_range) / max(250.0, move_speed)
        at_hit = tr.predict_hp(now, FAST.lasthit_lead_s + walk + windup) * hp_max
        # The forecast may bring the minion down by about one allied hit, no more: counting on
        # more, Q at 25-45% HP paid 0/5 and autos at 20-30% 0/3 (game 4), against 6/6 at 10-20%.
        now_abs = tr.unit.hp * hp_max
        # Reach: an auto range plus ~1 s of walking (counted in the forecast). Over g18 only 34 of 313
        # low enemy minions were within 300 units of Yasuo while farming; 57% were past 500.
        tr.last_dist = d
        if d <= VC.auto_range + 380 and 0 < at_hit <= ad * FAST.lasthit_margin and now_abs - ad <= FAST.forecast_cap:
            tr.at_hit = at_hit
            tr.was_killable = True
            tr.block = "farm step not reached"  # farm_step overwrites this when it gets to the minion
            sc.killable_auto.append(tr)
        if tr not in sc.killable_auto and tr.hp_rate(now) < 0 and 0 < tr.predict_hp(now, 1.6) * hp_max <= ad * 1.1:
            sc.soon_killable.append(tr)
        if e_dmg and d <= VC.e_range and tr.e_marked_until <= now:
            at_e = tr.predict_hp(now, FAST.lasthit_lead_s + 0.15) * hp_max
            if 0 < at_e <= e_dmg and now_abs - e_dmg <= FAST.forecast_cap:
                tr.at_e = at_e
                tr.was_killable = True
                sc.killable_e.append(tr)
        at_q = tr.predict_hp(now, FAST.lasthit_lead_s + FAST.q_cast_s) * hp_max
        if q_rank and d <= VC.q_range and 0 < at_q <= q_dmg and now_abs - q_dmg <= FAST.forecast_cap:
            tr.at_q = at_q
            tr.was_killable = True
            sc.killable_q.append(tr)
    # Most HP left at the moment of our hit first: the nearly dead ones are the ones allied minions
    # finish before the hit lands (autos at 0-5% paid 1 in 5, game 4).
    sc.killable_auto.sort(key=lambda t: (-getattr(t, "at_hit", 0.0), sc.dist(t)))
    sc.soon_killable.sort(key=lambda t: t.predict_hp(now, 1.2))
    # Q too: the most HP left that Q still kills first (g06: Q at 30% paid 9/11, at 0-15% 5/23,
    # the nearly dead ones fall to allied minions during the cast).
    sc.killable_q.sort(key=lambda t: -getattr(t, "at_q", 0.0))
    if sc.champ is not None and sc.champ_dist is not None and sc.champ_dist > VC.e_range * 0.8:
        # A minion within E range whose far side is closer to the champion than we are now.
        cx, cy = sc.champ.unit.x, sc.champ.unit.y
        opts = []
        for tr in minions:
            if tr.e_marked_until > now or sc.dist(tr) > VC.e_range:
                continue
            dx, dy = tr.unit.x - me_xy[0], tr.unit.y - me_xy[1]
            n = math.hypot(dx, dy) or 1.0
            land = (me_xy[0] + dx / n * VC.e_range * VC.px_per_unit, me_xy[1] + dy / n * VC.e_range * VC.px_per_unit)
            g = sc.champ_dist - math.hypot(land[0] - cx, land[1] - cy) / VC.px_per_unit
            if g > 150:
                opts.append((tr, g))
        opts.sort(key=lambda o: -o[1])
        sc.dash_options = opts[:4]
        if opts:
            sc.dash_toward = opts[0][0]
    return sc


class Micro:
    """Executes one tactical option at a time, plus reflexes, with an order-rate cap."""

    def __init__(self, ctl: Controller, screen: Screen, kb: Keybinds, side: str) -> None:
        self.ctl, self.screen, self.kb, self.side = ctl, screen, kb, side
        f = 1 / math.sqrt(2)
        self.fwd = (f, -f) if side == "ORDER" else (-f, f)   # screen direction up the lane; the loop updates it per lane
        self.last_order = 0.0
        self.last_attack = 0.0
        self.last_move = 0.0
        self.mode = "farm"
        self.mode_since = 0.0
        self.summoners: list[str | None] = []   # set by the loop each tick (flash, ignite, ...)
        self.hp_pct = 100.0                     # own HP, set by the loop each tick
        self._summoner_cast_at: dict[int, float] = {}
        self.attacked_ids: dict[int, float] = {}   # track id -> last time we attacked or Q'd it
        self.last_action = ""
        self.last_seq = 0
        self.orders = 0
        self.react_ms: collections.deque[tuple[str, float]] = collections.deque(maxlen=200)
        self._later: list[tuple[float, object]] = []
        self.last_exec: dict | None = None
        self.lh_pending: list[tuple[float, str, float]] = []   # (time, kind, minion HP fraction) awaiting a gold check
        self.at_camp = False                                   # the loop sets it while clearing a jungle camp
        self.fight_owned_until = 0.0                           # the fight head owns the mode until then
        self.dodge_lines = True                                # the lane opponent throws line skillshots (sidestep)
        self.dash_ok = True                                    # farm dashes allowed (not near their tower)
        self.last_action_t = 0.0
        self.plan_log: collections.deque = collections.deque(maxlen=400)  # lane planner picks, for the game log
        self.ad, self.aspd = 0.0, 0.7                          # set each tick (our hits' damage for measuring minions)
        self.trade_cooldown_until = 0.0                        # no new trade before then (one just ended)
        self.flash_in_ok = False                               # the fight head says a Flash-in kill is on

    def set_mode(self, mode: str, now: float) -> None:
        if mode != self.mode:
            self.mode, self.mode_since = mode, now

    def summoner_slot(self, name: str, ready: dict) -> int | None:
        now = time.time()
        for i, (n, hud) in enumerate(zip(self.summoners, "DF"), 1):
            # A summoner just cast still reads ready for a few frames: ignite went out five times in
            # one second (g18). Two seconds after a cast it counts as used.
            if n == name and ready.get(hud) and now - self._summoner_cast_at.get(i, 0.0) > 2.0:
                return i
        return None

    def cast_summoner(self, slot: int, x: float, y: float) -> None:
        self._summoner_cast_at[slot] = time.time()
        self.ctl.cast(self.kb.summoner(slot), *self._pt(x, y), self.kb.quick(f"evtCastAvatarSpell{slot}"))

    def later(self, delay_s: float, fn) -> None:
        """Queue a combo step to run `delay_s` from now (checked every actor tick)."""
        self._later.append((time.time() + delay_s, fn))

    def run_due(self, now: float) -> bool:
        due = [f for t, f in self._later if t <= now]
        self._later = [(t, f) for t, f in self._later if t > now]
        for fn in due:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        return bool(due)

    # -- helpers -------------------------------------------------------------------------
    def _pt(self, x: float, y: float) -> tuple[float, float]:
        x = min(max(x, 95), self.screen.px_w - 10)
        y = min(max(y, 75), config.VISION.view[3] - 5)
        return self.screen.to_points(x, y)

    def _can_order(self, now: float) -> bool:
        return now - self.last_order >= FAST.min_action_gap_s

    FIGHT_WORDS = ("trade:", "all_in:", "R ", "ignite", "Q3 tornado", "E+Q", "escape", "Smite", "flash", "Flash", "W wind wall",
                   "Sonic Wave", "Q2 dash", "R kick", "E Tempest")
    on_fight_order = None  # the loop sets a callback that writes fight orders to the event log

    def _ordered(self, now: float, what: str) -> None:
        self.last_order = now
        self.last_action = what
        self.last_action_t = now
        if self.on_fight_order is not None and any(k in what for k in self.FIGHT_WORDS):
            self.on_fight_order(what)
        self.orders += 1

    def attack_ready(self, now: float, attack_speed: float) -> bool:
        return now - self.last_attack >= 1.0 / max(0.3, attack_speed)

    def in_windup(self, now: float, attack_speed: float) -> bool:
        return now - self.last_attack < FAST.windup_frac / max(0.3, attack_speed) + 0.05

    def attack(self, tr: Track, now: float, what: str, right_click: bool = False) -> None:
        # Attack-move click on the unit (A, then left click): the game attacks the unit nearest the
        # cursor, so it lands even when the click point is a little off the model or the cursor has
        # not been registered over the unit yet (a bare right-click then becomes a move order).
        # Jungle monsters get a right-click on the body: an attack-move does not start a fight with
        # a camp that is not already fighting us.
        pt = self._pt(tr.unit.x, tr.unit.y)
        self.attacked_ids[tr.id] = now
        if tr.unit.kind == "minion" and self.ad:
            # lands after the input and the wind-up (Yasuo is melee: no projectile)
            tr.hits_expected.append((now + FAST.lasthit_lead_s + FAST.windup_frac / max(0.3, self.aspd), self.ad))
        if right_click or self.at_camp or tr.unit.kind == "monster":
            self.ctl.click(*pt, "right")
        else:
            self.ctl.attack_move(self.kb.attack_move, *pt)
        self.last_attack = now
        self._ordered(now, what)
        self.last_exec = {"name": what, "what": what, "ts": now, "target": pt, "point": None}

    def move_screen(self, x: float, y: float, now: float, what: str, every: float = 0.25) -> None:
        if now - self.last_move < every:
            return
        self.ctl.move_to(*self._pt(x, y))
        self.last_move = now
        # A move does not close the order gate (its own `every` spaces moves): holding, backing out and
        # sidestepping every 0.15-0.3 s kept the 0.11 s gate shut when minions became killable, and 22
        # last hits in six minutes were "not taken (order rate limit)" (g29).
        last = self.last_order
        self._ordered(now, what)
        self.last_order = last

    def cast(self, idx: int, x: float, y: float) -> None:
        """Ability `idx` (1-4) at a screen point, honouring the player's quick-cast setting."""
        self.ctl.cast(self.kb.ability(idx), *self._pt(x, y), self.kb.quick_cast(idx))

    def reacted(self, kind: str, since_ts: float) -> None:
        """Record screen-to-input latency: `since_ts` is when the frame behind this order was read."""
        self.react_ms.append((kind, (time.time() - since_ts) * 1000))

    def latency(self, kind: str) -> tuple[float, float] | None:
        xs = sorted(ms for k, ms in self.react_ms if k == kind)
        if not xs:
            return None
        return xs[len(xs) // 2], xs[int(len(xs) * 0.9) - 1 if len(xs) > 1 else 0]

    # -- behaviours ------------------------------------------------------------------------
    def farm_step(self, sc: Scene, now: float, attack_speed: float, push: bool = False) -> bool:
        """Last-hit what is about to die, otherwise stand just behind the minion front.
        Returns False when there is nothing on screen to farm (macro movement takes over)."""
        if not sc.minions:
            return False
        ready = self.attack_ready(now, attack_speed)
        for t in sc.killable_auto:
            t.block = "auto on cooldown" if not ready else ("in wind-up" if self.in_windup(now, attack_speed) else "not first pick")
        if sc.killable_auto and ready:
            self.attack(sc.killable_auto[0], now, f"last hit ({int(sc.killable_auto[0].unit.hp * 100)}%)")
            self.lh_pending.append((now, f"auto-{getattr(sc.killable_auto[0], 'role', '') or '?'}", sc.killable_auto[0].unit.hp))
            return True
        if self.in_windup(now, attack_speed):
            return True
        if push and self.attack_ready(now, attack_speed) and not sc.soon_killable:
            # (An auto spent on the wave is not ready for the minion about to be killable.)
            tgt = min(sc.minions, key=lambda t: t.unit.hp)
            self.attack(tgt, now, "push: attack lowest minion")
            return True
        alone = sc.allies == 0 and len(sc.minions) >= 3  # our wave is gone and theirs is here: every minion targets me
        if sc.soon_killable and not (alone and self.hp_pct < 60):
            # About to be last-hittable: be in range when it is, so the hit needs no walk.
            tr = sc.soon_killable[0]
            if sc.dist(tr) > VC.auto_range - 20:
                mx, my = sc.me_xy
                dx, dy = tr.unit.x - mx, tr.unit.y - my
                n = math.hypot(dx, dy) or 1.0
                stand = (VC.auto_range - 60) * VC.px_per_unit
                self.move_screen(tr.unit.x - dx / n * stand, tr.unit.y - dy / n * stand, now, "farm: step up for a last hit", every=0.15)
            return True
        # The wave's front line is the enemy minion furthest toward OUR side (smallest projection on
        # the lane direction), not the one nearest to us: inside the wave, the nearest one is next to
        # us and "behind" it is still inside. Stand an attack range behind the front line.
        mx, my = sc.me_xy
        along = lambda t: (t.unit.x - mx) * self.fwd[0] + (t.unit.y - my) * self.fwd[1]
        front = min(sc.minions, key=along)
        # An auto range behind their front line, or just inside it when their champion is not close:
        # the minions that die are the ones fighting at the front.
        # With her close, wait further back and step up only for the last hit: holding an auto range
        # behind the front line left Yasuo in a mage's reach, and Annie's poke and burst took most of
        # his deaths (g24).
        # (Only when hurt: always waiting 440 back with her in lane dropped Yasuo to 20 CS at 15:00, g26.)
        close = sc.champ is not None and (sc.champ_dist or 9e9) < 900
        back = (VC.auto_range + ((200 if self.hp_pct < 60 else 40) if close else -40)) * VC.px_per_unit
        if alone:
            # Out of their minions' reach until our wave arrives: standing an auto range behind seven of
            # them with none of ours cost 55% -> 0 at level 2 (g16).
            back = 700 * VC.px_per_unit
        tx, ty = front.unit.x - self.fwd[0] * back, front.unit.y - self.fwd[1] * back
        ahead = along(front) < 0  # we are past the enemy front line: get out now
        if not ahead and self.dodge_lines and sc.champ is not None and (sc.champ_dist or 9e9) < 1150:
            # Their champion is in skillshot range: never stand still while waiting. Bots aim at where
            # we stand; sidestepping across the lane every 0.5-0.9 s makes the straight lines miss.
            if now >= getattr(self, "_juke_flip_at", 0.0):
                self._juke_side = -getattr(self, "_juke_side", 1)
                self._juke_flip_at = now + random.uniform(0.5, 0.9)
            side = (-self.fwd[1], self.fwd[0])
            off = 120 * VC.px_per_unit * self._juke_side
            self.move_screen(tx + side[0] * off, ty + side[1] * off, now, "farm: sidestep (champion in range)", every=0.2)
            return True
        if ahead or math.hypot(tx - mx, ty - my) > 35:
            self.move_screen(tx, ty, now, "farm: back out of the wave" if ahead else "farm: hold behind the wave",
                             every=0.15 if ahead else 0.3)
        return True

    def back_off(self, sc: Scene, now: float) -> None:
        mx, my = sc.me_xy
        self.move_screen(mx - self.fwd[0] * 260, my - self.fwd[1] * 260, now, "back off", every=0.2)

    def support_step(self, sc: Scene, now: float, attack_speed: float) -> bool:
        """Support positioning: stay beside the carry, a little toward the enemy; never take last hits."""
        if sc.ally_champs:
            carry = min(sc.ally_champs, key=lambda u: math.hypot(u.x - sc.me_xy[0], u.y - sc.me_xy[1]))
            side = (-self.fwd[1], self.fwd[0])
            tx = carry.x + self.fwd[0] * 70 + side[0] * 60
            ty = carry.y + self.fwd[1] * 70 + side[1] * 60
            if math.hypot(tx - sc.me_xy[0], ty - sc.me_xy[1]) > 45:
                self.move_screen(tx, ty, now, "support: beside the carry", every=0.3)
            return True
        if sc.minions:
            front = min(sc.minions, key=sc.dist)
            back = (VC.auto_range + 380) * VC.px_per_unit
            tx, ty = front.unit.x - self.fwd[0] * back, front.unit.y - self.fwd[1] * back
            if math.hypot(tx - sc.me_xy[0], ty - sc.me_xy[1]) > 45:
                self.move_screen(tx, ty, now, "support: hold behind the wave", every=0.3)
            return True
        return False
