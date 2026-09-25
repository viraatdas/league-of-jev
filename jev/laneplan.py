"""Lane planner: every option this tick scored on one scale, the best one taken.

The rule stack before it fired each action only when its own gate was open and fell back to a
move otherwise: 68-79% of Yasuo's orders were moves and E was used 1-15 times a game (g22-g29).
Here autos, Q, E, E+Q, hits on the champion and a handful of standing spots all compete every
tick in gold-equivalents:

    last hit       P(kill) x minion gold (+ minions the same Q line or circle also kills)
    Q stack        a Q that hits units while the tornado is not up yet
    E dash         the landing spot's value minus the current spot's, plus what it kills
    champion hit   her HP taken, weighted by Jev's aggression, minus her wave's aggro
    standing spot  gold within reach soon, minus her threat range, their tower, their wave

P(kill) is soft (a logistic on predicted HP at the hit against our damage), so a 50/50 last hit
is worth half a minion and a sure one a whole one. Every choice is logged with its runner-ups
(Micro.plan_log), so the weights below are tuned from games, not guessed twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from jev import config

VC, FAST = config.VISION, config.FAST

GOLD = {"melee": 21.0, "caster": 14.0, "": 17.0}
HP_GOLD = 1.2            # gold-equivalent of 1% of my HP in lane (more when low, below)
CHAMP_HP_GOLD = 1.6      # gold-equivalent of 1% of her HP at aggression 1
Q_STACK = 4.0            # a Q stack toward the tornado
E_COST = 2.0             # an E is cheap (0.5 s cooldown), but the target locks for 10 s (moot when it dies)
Q_COST = 4.0             # Q on a minion: ~3.5 s without the trade tool (E's cooldown is 0.5 s)
E_MOVE_MIN = 6.0         # a dash that kills nothing must reach a clearly better spot
E_MIN = 2.0              # a dash worth less than this is noise (it moves me for nothing)
Q_COST_NEAR_HER = 5.0    # Q on cooldown when she walks in: the trade we cannot take
MOVE_GAIN_MIN = 2.5      # a new spot must be this much better than standing still


@dataclass
class Option:
    kind: str                                   # auto | q | e | eq | auto_champ | q_champ | eq_champ | move | hold
    value: float
    target: object = None                       # Track
    point: tuple[float, float] | None = None    # screen px
    why: str = ""
    p: float = 0.0                              # chance the hit kills its minion (last hits)
    z: float | None = None                      # the predicted margin behind p (the learner fits p from it)

    def brief(self) -> dict:
        return {"kind": self.kind, "value": round(self.value, 1), "why": self.why}


def margin(dmg: float, hp_at_hit: float, hp_max: float) -> float | None:
    """Damage over predicted HP at the hit, in units of 4% of the minion's max HP (None: no kill)."""
    if dmg <= 0 or hp_at_hit <= 0:
        return None
    return (dmg - hp_at_hit) / max(1.0, 0.04 * hp_max)


def p_kill(dmg: float, hp_at_hit: float, hp_max: float) -> float:
    """Soft chance that `dmg` kills a minion predicted at `hp_at_hit`: 0.5 at equality, 0.92 with a
    10%-of-max margin. Dead before the hit (<= 0) is someone else's kill."""
    if dmg <= 0 or hp_at_hit <= 0:
        return 0.0
    z = (dmg - hp_at_hit) / max(1.0, 0.04 * hp_max)
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _px(units: float) -> float:
    return units * VC.px_per_unit


def _units(px: float) -> float:
    return px / VC.px_per_unit


class LanePlanner:
    """Scores the options of one tick. `kit` supplies Yasuo's Q stacks (kit.q) when it has them."""

    def __init__(self, kit) -> None:
        self.kit = kit

    # -- helpers -------------------------------------------------------------------------
    def _pk(self, mi, kind: str, dmg: float, at: float, hp_max: float) -> tuple[float, float | None]:
        """Kill chance by the curve learned this game for this hit kind (the fixed one without a learner)."""
        z = margin(dmg, at, hp_max)
        if z is None:
            return 0.0, None
        lr = getattr(mi, "learner", None)
        return (lr.p_kill(kind, z) if lr is not None else p_kill(dmg, at, hp_max)), z

    def _hp_value(self, hp_pct: float) -> float:
        """Gold-equivalent of 1% HP: worth more the lower I am."""
        return HP_GOLD * (1.0 + max(0.0, 60.0 - hp_pct) / 30.0)

    def _at(self, tr, now: float, delay: float) -> float:
        return tr.predict_hp(now, delay) * getattr(tr, "hp_max", 400.0)

    def _gold(self, tr) -> float:
        return GOLD.get(getattr(tr, "role", "") or "", 17.0)

    def _dist_px(self, a, b) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _along(self, p, me, fwd) -> float:
        return (p[0] - me[0]) * fwd[0] + (p[1] - me[1]) * fwd[1]

    def _line_units(self, sc, x: float, y: float, rng: float, width_px: float = 45.0) -> list:
        mx, my = sc.me_xy
        dx, dy = x - mx, y - my
        n = math.hypot(dx, dy) or 1.0
        out = []
        for tr in sc.minions:
            px, py = tr.unit.x - mx, tr.unit.y - my
            along = (px * dx + py * dy) / n
            perp = abs(px * dy - py * dx) / n
            if 0 <= along <= _px(rng) and perp <= width_px:
                out.append(tr)
        return out

    def _line_passes(self, sc, x: float, y: float, rng: float, u, width_px: float = 55.0) -> bool:
        """Does the line from me toward (x, y) pass over unit `u`?"""
        mx, my = sc.me_xy
        dx, dy = x - mx, y - my
        n = math.hypot(dx, dy) or 1.0
        px, py = u.x - mx, u.y - my
        along = (px * dx + py * dy) / n
        return 0 <= along <= _px(rng) and abs(px * dy - py * dx) / n <= width_px

    def _exit_after(self, sc, mi, land: tuple[float, float], used, now: float) -> bool:
        """Lookahead: from `land`, another minion to dash through back toward home (at least 250
        units gained toward our side), so E in, Q, E out is one plan and not a stranded dash."""
        home = (-mi.fwd[0], -mi.fwd[1])
        r = _px(VC.e_range)
        for m in sc.minions:
            if m is used or m.e_marked_until > now:
                continue
            dx, dy = m.unit.x - land[0], m.unit.y - land[1]
            n = math.hypot(dx, dy)
            if 0 < n <= r and (dx / n * home[0] + dy / n * home[1]) * VC.e_range >= 250:
                return True
        return False

    def _follow_up(self, sc, mi, land: tuple[float, float], q_left: bool, w: float, aggro_cost: float, her_hp_units: float) -> float:
        """Lookahead: the best hit on her from `land` right after the dash (Q if still up, else an auto)."""
        ch = sc.champ
        if ch is None:
            return 0.0
        dl = _units(self._dist_px(land, (ch.unit.x, ch.unit.y)))
        if q_left and dl <= VC.q_range + 20:
            return max(0.0, sc.q_dmg / 0.88 / her_hp_units * 100 * w - aggro_cost)
        if dl <= VC.auto_range + 60:
            return max(0.0, sc.ad / her_hp_units * 100 * w - aggro_cost)
        return 0.0

    def _landing(self, sc, tr) -> tuple[float, float]:
        mx, my = sc.me_xy
        dx, dy = tr.unit.x - mx, tr.unit.y - my
        n = math.hypot(dx, dy) or 1.0
        r = _px(VC.e_range)
        return mx + dx / n * r, my + dy / n * r

    # -- the value of standing somewhere -------------------------------------------------
    def spot_value(self, sc, mi, s: tuple[float, float], now: float, reach_scale: float = 1.0) -> tuple[float, str]:
        """Gold within reach over the next ~2 s, minus the threats of standing at `s` (her reach
        scaled by `reach_scale`: with a dash out ready, a moment in her reach costs less)."""
        lane = sc.lane
        farm = 0.0
        reach = VC.auto_range + 60
        for tr in sc.minions:
            if getattr(tr, "ally_takes", False):
                continue
            soon = self._at(tr, now, 1.5)
            if soon > sc.ad * 1.3 and tr.unit.hp > 0.35:
                continue
            d = _units(self._dist_px(s, (tr.unit.x, tr.unit.y)))
            r = 1.0 if d <= reach else max(0.0, 1.0 - (d - reach) / 300.0)
            farm += r * self._gold(tr) * (0.8 if soon <= sc.ad * 1.3 else 0.25)
        risk = 0.0
        notes = []
        ch = sc.champ
        if ch is not None:
            # her reach with the spells she has up (enemies.py): Brand's Q 1050, Nasus's slow 700
            her_reach = float(lane.get("opp_reach", float(lane.get("opp_range", 550.0)) + 150.0))
            if lane.get("cautious"):
                her_reach += 150.0   # backing off: a margin past her reach, and her reach counts double below
            d = _units(self._dist_px(s, (ch.unit.x, ch.unit.y)))
            if d < her_reach:
                ahead = mi.hp_pct - ch.unit.hp * 100
                factor = 1.4 if ahead < -10 else (1.0 if ahead < 15 else 0.4)
                lr = getattr(mi, "learner", None)
                rate = lr.in_reach if lr is not None else 3.0  # % HP a second she takes while I stand in reach
                pen = (her_reach - d) / her_reach * rate * 2.5 * factor * self._hp_value(mi.hp_pct) * reach_scale
                if lane.get("cautious"):
                    pen *= 2.0
                risk += pen
                notes.append(f"her reach {pen:.0f}")
        tower = lane.get("tower_px")
        if tower is not None and not lane.get("tower_farm_ok"):
            d = _units(self._dist_px(s, tower))
            if d < 950:
                risk += 40.0 + (950 - d) / 10.0
                notes.append("their tower")
        # Three of them unseen: past the middle of the lane is where the gank lands (g19, g26).
        lim = lane.get("mia_limit_px")
        if lim is not None:
            a = self._along(s, sc.me_xy, mi.fwd)
            if a > lim:
                risk += 6.0 + _units(a - lim) / 60.0
                notes.append("past the middle, 3+ unseen")
        # Their wave: standing in front of our own minions takes the aggro of every one of theirs.
        fwd = mi.fwd
        if sc.minions:
            enemy_front = min(self._along((t.unit.x, t.unit.y), sc.me_xy, fwd) for t in sc.minions)
            ally_front = max((self._along((u.x, u.y), sc.me_xy, fwd) for u in sc.ally_units), default=None)
            a = self._along(s, sc.me_xy, fwd)
            if a > enemy_front + _px(40):
                # Minions hit minions before champions: inside the wave is cheap while ours are there
                # to take the shots, and every minion's target when they are not.
                risk += 3.0 if ally_front is not None else 12.0
                notes.append("inside their wave")
            elif ally_front is None and a > enemy_front - _px(500):
                close = sum(1 for t in sc.minions if _units(self._dist_px(s, (t.unit.x, t.unit.y))) < 520)
                if close:
                    risk += 3.0 * close
                    notes.append("their wave, none of ours")
        return farm - risk, ", ".join(notes)

    def _spots(self, sc, mi) -> list[tuple[float, float]]:
        """Candidate spots: behind their front line at a few depths and to both sides, and here."""
        me = sc.me_xy
        out = [me]
        if not sc.minions:
            return out
        fwd = mi.fwd
        front = min(sc.minions, key=lambda t: self._along((t.unit.x, t.unit.y), me, fwd))
        fx, fy = front.unit.x, front.unit.y
        side = (-fwd[1], fwd[0])
        for back in (VC.auto_range - 60, VC.auto_range + 60, VC.auto_range + 220, 520.0, 750.0):
            for lat in (0.0, -140.0, 140.0):
                out.append((fx - fwd[0] * _px(back) + side[0] * _px(lat), fy - fwd[1] * _px(back) + side[1] * _px(lat)))
        return out

    # -- options ---------------------------------------------------------------------------
    def options(self, sc, mi, now: float, pushing: bool) -> list[Option]:
        lane = sc.lane
        out: list[Option] = [Option("hold", 0.0, why="nothing better")]
        here, here_note = self.spot_value(sc, mi, sc.me_xy, now)
        ready = sc.ready
        q = getattr(self.kit, "q", None)
        q3 = bool(q.q3(now)) if q is not None else False
        ch, cd = sc.champ, (sc.champ_dist or 9e9)
        windup = FAST.windup_frac / max(0.3, sc.aspd)
        attack_ready = mi.attack_ready(now, sc.aspd)
        agg = float(lane.get("aggression", 1.0))
        hp_val = self._hp_value(mi.hp_pct)
        her_wave = sum(1 for t in sc.minions if ch is not None and _units(self._dist_px((t.unit.x, t.unit.y), (ch.unit.x, ch.unit.y))) < 500)
        # her wave turns on me when I hit her: each of its minions takes a learned share of my HP
        lr = getattr(mi, "learner", None)
        aggro_cost = her_wave * (lr.aggro if lr is not None else 1.5) * hp_val
        trade_w = lr.trade_weight() if lr is not None else 1.0
        dash_cost = 0.5 * (lr.dash if lr is not None else 1.0) * hp_val
        her_hp_units = float(lane.get("opp_hp", 700.0))

        # Autos on minions (auto_p: what a Q or E kill on the same minion adds over the free auto)
        auto_p: dict[int, float] = {}
        if attack_ready:
            for tr in sc.minions:
                if getattr(tr, "ally_takes", False):
                    continue
                d = sc.dist(tr)
                walk = max(0.0, d - VC.auto_range) / max(250.0, sc.move_speed)
                if walk > 1.0:
                    continue
                at = self._at(tr, now, FAST.lasthit_lead_s + walk + windup)
                p, z = self._pk(mi, "auto", sc.ad * FAST.lasthit_margin, at, getattr(tr, "hp_max", 400.0))
                if walk == 0.0:
                    auto_p[tr.id] = p
                v = p * self._gold(tr) - walk * 3.0
                if p < 0.15 and pushing and tr.unit.hp > 0.2:
                    v = 2.0 - walk * 3.0  # pushing: hit the wave (not the minion about to be last-hittable)
                if v > 0.5:
                    out.append(Option("auto", v, tr, why=f"p={p:.2f} walk={walk:.1f}s", p=p, z=z))

        # Q on minions (a line), or the tornado through the wave when pushing. Q is on cooldown for
        # ~3.5 s after: the last hits it would have taken in that time are its cost (a Q spent on
        # a full minion for a stack left none for the one about to die).
        q_later = 0.0
        if ready.get("Q") and sc.q_dmg > 0:
            for u in sc.minions:
                if getattr(u, "ally_takes", False) or sc.dist(u) > VC.q_range + 150:
                    continue
                soon = self._at(u, now, 2.0)
                if 0 < soon <= sc.q_dmg and self._at(u, now, FAST.lasthit_lead_s + FAST.q_cast_s) > sc.q_dmg:
                    q_later = max(q_later, 0.6 * self._gold(u))
        if ready.get("Q") and sc.q_dmg > 0:
            rng = VC.q3_range * 0.8 if q3 else VC.q_range
            for tr in sc.minions:
                if sc.dist(tr) > rng:
                    continue
                line = self._line_units(sc, tr.unit.x, tr.unit.y, rng)
                qp = {u.id: self._pk(mi, "Q", sc.q_dmg, self._at(u, now, FAST.lasthit_lead_s + FAST.q_cast_s), getattr(u, "hp_max", 400.0))
                      for u in line if not getattr(u, "ally_takes", False)}
                gold = sum(qp[u.id][0] * self._gold(u) * (0.3 if auto_p.get(u.id, 0.0) >= 0.7 else 1.0)
                           for u in line if u.id in qp)
                v = gold + (Q_STACK if (line and not q3) else 0.0) - q_later - Q_COST
                if q3 and not pushing and gold < 10:
                    v -= 8.0  # the tornado on a wave that dies anyway: keep it for her
                if q3 and ch is not None and cd <= VC.q3_range:
                    v -= 30.0  # the tornado is for her
                if ch is not None and cd < 800:
                    v -= Q_COST_NEAR_HER * agg
                if ch is not None and self._line_passes(sc, tr.unit.x, tr.unit.y, rng, ch.unit) and her_wave >= 3 and ch.unit.hp > 0.3:
                    v -= aggro_cost  # the Q also hits her: her whole wave turns on me (g16)
                if v > 1.0:
                    p, z = qp.get(tr.id, (0.0, None))
                    out.append(Option("q", v, tr, why=f"line {len(line)} gold {gold:.0f}", p=p, z=z))

        # E through a minion (a last hit, a better spot, and with Q up the circle Q in the dash),
        # looked at one step further: the hit on her from the landing spot, and a dash back out.
        e_ok = ready.get("E") and sc.e_dmg > 0 and getattr(mi, "dash_ok", True)
        champ_w = 0.0
        if ch is not None and agg > 0.2:
            # Trading while well behind in HP loses the exchange that follows it: she answers and wins.
            behind = ch.unit.hp * 100 - mi.hp_pct
            fac = 1.0 if behind <= 10 else (0.6 if behind <= 25 else 0.25)
            if mi.hp_pct < 35:
                fac *= 0.3
            if lane.get("her_spells_down"):
                fac *= 1.4  # her burst is on cooldown: the window every laner trades in
            champ_w = CHAMP_HP_GOLD * agg * trade_w * fac * (1.3 if ch.unit.hp < 0.4 else 1.0)
        if e_ok:
            for tr in sc.minions:
                d = sc.dist(tr)
                if d > VC.e_range or tr.e_marked_until > now:
                    continue
                land = self._landing(sc, tr)
                exit_ok = ch is not None and self._exit_after(sc, mi, land, tr, now)
                spot, note = self.spot_value(sc, mi, land, now, reach_scale=0.4 if exit_ok else 1.0)
                follow = 0.8 * self._follow_up(sc, mi, land, bool(ready.get("Q")) and not q3, champ_w, aggro_cost, her_hp_units) if champ_w else 0.0
                if not exit_ok:
                    follow *= 0.5  # in with no way back out: she answers the hit where I stand
                if exit_ok:
                    note = (note + ", " if note else "") + "dash out ready"
                at = self._at(tr, now, FAST.lasthit_lead_s + 0.15)
                pe, ze = (0.0, None) if getattr(tr, "ally_takes", False) else self._pk(mi, "E", sc.e_dmg, at, getattr(tr, "hp_max", 400.0))
                kill = pe * self._gold(tr)
                if auto_p.get(tr.id, 0.0) >= 0.7:
                    kill *= 0.3  # the auto takes it for free
                pk = kill / self._gold(tr)
                v = kill + 0.6 * (spot - here) - (0.5 + E_COST * (1.0 - pk)) - dash_cost
                if pk < 0.3 and spot - here < E_MOVE_MIN and follow < E_MOVE_MIN:
                    v = min(v, 0.0)
                v_e = v + follow
                if v_e >= E_MIN:
                    out.append(Option("e", v_e, tr, land, why=f"kill {kill:.0f} spot {spot - here:+.0f} then {follow:.0f} {note}", p=pk, z=ze))
                if ready.get("Q") and not q3:
                    r = _px(FAST.eq_radius)
                    around = [u for u in sc.minions if u is not tr and self._dist_px((u.unit.x, u.unit.y), land) <= r]
                    if around:
                        qg = sum(self._pk(mi, "Q", sc.q_dmg, self._at(u, now, FAST.lasthit_lead_s + 0.25), getattr(u, "hp_max", 400.0))[0]
                                 * self._gold(u) for u in around if not getattr(u, "ally_takes", False))
                        v2 = v + qg + Q_STACK - q_later - Q_COST - (Q_COST_NEAR_HER * agg if (ch is not None and cd < 800) else 0.0)
                        if champ_w and ch is not None and self._dist_px((ch.unit.x, ch.unit.y), land) <= r:
                            v2 += sc.q_dmg / 0.88 / her_hp_units * 100 * champ_w - aggro_cost  # the circle catches her too
                        elif champ_w:
                            v2 += 0.8 * self._follow_up(sc, mi, land, False, champ_w, aggro_cost, her_hp_units)
                        out.append(Option("eq", v2, tr, land, why=f"kill {kill:.0f} circle {len(around)} gold {qg:.0f} spot {spot - here:+.0f}", p=pk, z=ze))

        # The champion: hits that cost her more than they cost me
        if ch is not None and agg > 0.2:
            ahead = mi.hp_pct - ch.unit.hp * 100
            w = champ_w
            if attack_ready and cd <= VC.auto_range + 40:
                v = sc.ad / her_hp_units * 100 * w - aggro_cost - (6.0 if ahead < -15 else 0.0)
                out.append(Option("auto_champ", v, ch, why=f"ahead {ahead:+.0f} her wave {her_wave}"))
            if ready.get("Q") and not q3 and cd <= VC.q_range + 20:
                v = sc.q_dmg / 0.88 / her_hp_units * 100 * w - aggro_cost + Q_STACK
                out.append(Option("q_champ", v, ch, why=f"her wave {her_wave}"))
            if ready.get("Q") and q3 and cd <= VC.q3_range * 0.9:
                v = (sc.q_dmg / 0.88 / her_hp_units * 100 + 12.0) * w - aggro_cost  # the knock-up sets up everything after it
                out.append(Option("q3_champ", v, ch, why=f"tornado, her wave {her_wave}"))
            if e_ok and ready.get("Q") and cd <= VC.e_range and ch.e_marked_until <= now:
                exit_ok = self._exit_after(sc, mi, self._landing(sc, ch), ch, now)
                v = (sc.e_dmg + sc.q_dmg) / 0.85 / her_hp_units * 100 * w - aggro_cost - ((4.0 if exit_ok else 10.0) if ahead < -10 else 0.0)
                out.append(Option("eq_champ", v, ch, why=f"ahead {ahead:+.0f} her wave {her_wave}{', dash out ready' if exit_ok else ''}"))

        # Where to stand
        best_spot, best_v, best_note = None, here, here_note
        for s in self._spots(sc, mi)[1:]:
            v, note = self.spot_value(sc, mi, s, now)
            v -= _units(self._dist_px(s, sc.me_xy)) / 400.0  # walking is a little exposure and a little time
            if v > best_v:
                best_spot, best_v, best_note = s, v, note
        if best_spot is not None and best_v - here >= MOVE_GAIN_MIN:
            out.append(Option("move", best_v - here, point=best_spot, why=f"spot {here:.0f} -> {best_v:.0f} {best_note}"))
        return out

    def choose(self, sc, mi, now: float, pushing: bool) -> tuple[Option, list[Option]]:
        opts = self.options(sc, mi, now, pushing)
        opts.sort(key=lambda o: -o.value)
        return opts[0], opts[:4]
