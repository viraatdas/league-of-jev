"""Micro: unit tracking, last-hit prediction, and the executor for Jev's tactical choices.

Everything here runs in the fast loop (30 Hz) on the latest screen read. Screen positions
come from health bars (vision.py); the own champion's screen position comes from its own
bar, so none of this depends on the camera lock.
"""
from __future__ import annotations

import collections
import itertools
import math
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

    def predict_hp(self, now: float, lead: float) -> float:
        return self.unit.hp + self.hp_rate(now) * lead


class UnitTracker:
    """Keeps identities across frames by nearest-neighbour matching of screen positions."""

    def __init__(self, max_jump_px: float = 55.0, ttl: float = 0.5) -> None:
        self.tracks: dict[int, Track] = {}
        self.max_jump = max_jump_px
        self.ttl = ttl

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
                tr.unit, tr.seen = u, now
            else:
                tr = Track(next(_ids), u, now)
                self.tracks[tr.id] = tr
            tr.hist.append((now, u.hp))
            tr.path.append((now, u.x, u.y))
            out.append(tr)
        for tid, tr in list(self.tracks.items()):
            if now - tr.seen > self.ttl:
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
    champ_dist: float | None = None
    killable_auto: list[Track] = field(default_factory=list)
    killable_q: list[Track] = field(default_factory=list)
    soon_killable: list[Track] = field(default_factory=list)   # one auto kills it within ~1.2 s: get in range now
    dash_toward: Track | None = None                          # minion to E through toward the champion
    dash_options: list[tuple[Track, float]] = field(default_factory=list)  # (minion, units gained toward champion)
    ready: dict[str, bool] = field(default_factory=dict)
    r_lit: bool = False

    def dist(self, tr: Track) -> float:
        return math.hypot(tr.unit.x - self.me_xy[0], tr.unit.y - self.me_xy[1]) / VC.px_per_unit


def build_scene(view: View, minions: list[Track], champs: list[Track], ad: float, q_rank: int, game_s: float, now: float, fallback_xy,
                aspd: float = 0.7, move_speed: float = 345.0) -> Scene:
    me_xy = (view.me.x, view.me.y) if view.me else fallback_xy
    sc = Scene(me_xy=me_xy, minions=minions, allies=len(view.allies("minion")), ally_champs=view.allies("champion"))
    sc.ready = dict(view.hud.ready)
    sc.r_lit = bool(view.hud.ready.get("R"))
    if champs:
        sc.champ = min(champs, key=sc.dist)
        sc.champ_dist = sc.dist(sc.champ)
    hp_max = minion_max_hp(game_s)
    q_dmg = (FAST.q_base[max(0, min(q_rank, 5) - 1)] + FAST.q_ad * ad) if q_rank else 0.0
    windup = FAST.windup_frac / max(0.3, aspd)
    for tr in minions:
        d = sc.dist(tr)
        # Forecast the minion's HP at the moment the hit lands: input, the walk into range, the
        # wind-up. With only the input lead, a hit that needed a walk landed after the allied
        # minions had taken the kill ("last hit (3%)" on a dying minion, game 2).
        walk = max(0.0, d - VC.auto_range) / max(250.0, move_speed)
        at_hit = tr.predict_hp(now, FAST.lasthit_lead_s + walk + windup) * hp_max
        if d <= VC.auto_range + 250 and 0 < at_hit <= ad * FAST.lasthit_margin:
            sc.killable_auto.append(tr)
        if tr not in sc.killable_auto and tr.hp_rate(now) < 0 and 0 < tr.predict_hp(now, 1.2) * hp_max <= ad * 1.1:
            sc.soon_killable.append(tr)
        at_q = tr.predict_hp(now, FAST.lasthit_lead_s + FAST.q_cast_s) * hp_max
        if q_rank and d <= VC.q_range and 0 < at_q <= q_dmg:
            sc.killable_q.append(tr)
    sc.killable_auto.sort(key=lambda t: t.unit.hp)
    sc.soon_killable.sort(key=lambda t: t.predict_hp(now, 1.2))
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
        self.last_action = ""
        self.last_seq = 0
        self.orders = 0
        self.react_ms: collections.deque[tuple[str, float]] = collections.deque(maxlen=200)
        self._later: list[tuple[float, object]] = []
        self.last_exec: dict | None = None
        self.lh_pending: list[tuple[float, str, float]] = []   # (time, kind, minion HP fraction) awaiting a gold check

    def set_mode(self, mode: str, now: float) -> None:
        if mode != self.mode:
            self.mode, self.mode_since = mode, now

    def summoner_slot(self, name: str, ready: dict) -> int | None:
        for i, (n, hud) in enumerate(zip(self.summoners, "DF"), 1):
            if n == name and ready.get(hud):
                return i
        return None

    def cast_summoner(self, slot: int, x: float, y: float) -> None:
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

    def _ordered(self, now: float, what: str) -> None:
        self.last_order = now
        self.last_action = what
        self.orders += 1

    def attack_ready(self, now: float, attack_speed: float) -> bool:
        return now - self.last_attack >= 1.0 / max(0.3, attack_speed)

    def in_windup(self, now: float, attack_speed: float) -> bool:
        return now - self.last_attack < FAST.windup_frac / max(0.3, attack_speed) + 0.05

    def attack(self, tr: Track, now: float, what: str) -> None:
        # Attack-move click on the unit (A, then left click): the game attacks the unit nearest the
        # cursor, so it lands even when the click point is a little off the model or the cursor has
        # not been registered over the unit yet (a bare right-click then becomes a move order).
        pt = self._pt(tr.unit.x, tr.unit.y)
        self.ctl.attack_move(self.kb.attack_move, *pt)
        self.last_attack = now
        self._ordered(now, what)
        self.last_exec = {"name": what, "what": what, "ts": now, "target": pt, "point": None}

    def move_screen(self, x: float, y: float, now: float, what: str, every: float = 0.25) -> None:
        if now - self.last_move < every:
            return
        self.ctl.move_to(*self._pt(x, y))
        self.last_move = now
        self._ordered(now, what)

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
        if sc.killable_auto and self.attack_ready(now, attack_speed):
            self.attack(sc.killable_auto[0], now, f"last hit ({int(sc.killable_auto[0].unit.hp * 100)}%)")
            self.lh_pending.append((now, "auto", sc.killable_auto[0].unit.hp))
            return True
        if self.in_windup(now, attack_speed):
            return True
        if push and self.attack_ready(now, attack_speed):
            tgt = min(sc.minions, key=lambda t: t.unit.hp)
            self.attack(tgt, now, "push: attack lowest minion")
            return True
        if sc.soon_killable:
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
        back = (VC.auto_range + 40) * VC.px_per_unit
        tx, ty = front.unit.x - self.fwd[0] * back, front.unit.y - self.fwd[1] * back
        ahead = along(front) < 0  # we are past the enemy front line: get out now
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
