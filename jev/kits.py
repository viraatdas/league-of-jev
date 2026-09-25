"""Champion kits: what each champion can do, how Jev is asked about it, how code executes it.

A kit supplies the tactical head's action menu (with availability filters, as in OpenAI Five's
action masks), the ability state shown to Jev, extra target heads, the executor for each
action, frame-level reflexes, level-up order, and the item profile. The loop, vision, micro
primitives and the three Jev heads are shared.
"""
from __future__ import annotations

import math
import time
from typing import Any

from jev import config
from jev.actions import NONE, POINT, UNIT, Ctx, Spec
from jev.items import ItemProfile
from jev.micro import Micro, Scene

VC = config.VISION
FAST = config.FAST

YASUO_ID, THRESH_ID = 157, 412
FIGHT_MODES = ("trade", "all_in", "poke")


class Kit:
    """Base kit. specs(ctx) lists the champion's own moves (abilities, combos, standing modes);
    the universal moves, summoner spells and items are added by the tactical head."""

    name = "?"
    champ_id = 0
    execute_ok = True  # the kill-secure reflex (a support keeps it too: an auto or ignite)
    default_role = "MIDDLE"
    skill_order: list[str] = []
    items: ItemProfile

    def __init__(self, role: str = "") -> None:
        self.role = (role or self.default_role).upper()

    @property
    def support(self) -> bool:
        return self.role == "UTILITY"

    def role_text(self, lane_name: str) -> str:
        what = "support" if self.support else "laner"
        return f"a {self.name} {what} in the {lane_name} lane"

    def specs(self, ctx: Ctx) -> list[Spec]:
        raise NotImplementedError

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        return {}

    def reflex(self, mi: Micro, sc: Scene, now: float, plan: dict) -> bool:
        return False

    # -- fighting ------------------------------------------------------------------------
    # Jev decides whether to fight (the trade / all_in modes, or the strategy head's intent);
    # the kit runs the combo at frame rate, one order per actor tick, from the HUD's readiness.
    def fight_modes(self, sc: Scene) -> list[Spec]:
        if sc.champ is None or (sc.champ_dist or 9e9) > 1400:
            return []
        return self.modes(
            ("trade", "Short trade on the enemy champion: my burst combo and an auto or two, then step back before they answer."),
            ("all_in", "Fight the enemy champion to the death: close the gap, full combo, ignite, and chase until they die."),
        )

    def step(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        """Standing behaviour for this tick: the fight combo in a fight mode, else continuous()."""
        if sc.champ is not None:
            self._champ_seen = now
        if mode == "back_off" and now - getattr(self, "_champ_seen", 0.0) > 2.0:
            # Nothing left to back off from: a back_off picked against a misread red buff stuck, and
            # Lee walked away from his half-dead camp through the lane routine (g05).
            mi.set_mode("farm", now)
            mode = "farm"
        if mode in FIGHT_MODES:
            if sc.champ is not None:
                self._fight_seen = now
                return self.fight(mi, sc, now, aspd, mode)
            if now - getattr(self, "_fight_seen", 0.0) > 1.5:
                mi.set_mode("farm", now)
                mode = "farm"
        return self.continuous(mi, sc, now, aspd, mode, pushing)

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        if mode == "poke":
            return self.poke(mi, sc, now, aspd)
        if mode == "trade" and self.trade_over(mi, sc, now):
            return True
        return self.hit_or_chase(mi, sc, now, aspd, mode)

    def poke(self, mi: Micro, sc: Scene, now: float, aspd: float) -> bool:
        """What reaches from here and nothing that walks or dashes in; farm otherwise."""
        return self.poke_auto(mi, sc, now, aspd) or self.continuous(mi, sc, now, aspd, "farm", False)

    def e_minion_damage(self, e_rank: int, ad: float, level: int) -> float:
        """Damage of a targeted E on a minion (0: the kit has no such E)."""
        return 0.0

    def poke_auto(self, mi: Micro, sc: Scene, now: float, aspd: float) -> bool:
        """An auto on the champion when she walks into my reach, a last hit first. Poke only threw
        skills, and Q was on cooldown from last hits in 92 of the 165 reads where Jev wanted a
        trade: Nasus farmed next to Yasuo for seven minutes and took no auto (g29)."""
        ch, d = sc.champ, sc.champ_dist or 9e9
        if (ch is None or d > VC.auto_range + 40 or sc.killable_auto or sc.soon_killable
                or self.minions_near_champ(sc) >= 3 or mi.hp_pct < ch.unit.hp * 100 - 10
                or not mi._can_order(now) or not mi.attack_ready(now, aspd)):
            return False
        mi.attack(ch, now, "poke: auto the champion")
        return True

    def escape(self, mi: Micro, sc: Scene, now: float, home: tuple[float, float]) -> bool:
        """A kit move that gets us away (a dash toward `home`, a screen direction); False if none."""
        return False

    @staticmethod
    def minions_near_champ(sc: Scene, radius_units: float = 500.0) -> int:
        """Enemy minions around the enemy champion: dashing in there draws the whole wave's aggro."""
        if sc.champ is None:
            return 0
        cx, cy = sc.champ.unit.x, sc.champ.unit.y
        r = radius_units * VC.px_per_unit
        return sum(1 for t in sc.minions if math.hypot(t.unit.x - cx, t.unit.y - cy) <= r)

    def trade_window(self, mi: Micro, sc: Scene, now: float, level_diff: int) -> str | None:
        """A fight mode to enter on our own when the moment is clearly good (Jev picked farm 88%
        of the time with the enemy champion in view, game 4), else None."""
        return None

    def trade_over(self, mi: Micro, sc: Scene, now: float) -> bool:
        """A trade ends 0.7 s after the burst (one more auto), or 2.5 s after it began; then walk
        back for 1.2 s and return to farming. True while the trade is winding down."""
        t0 = mi.mode_since
        burst = getattr(self, "burst_at", 0.0)
        # Winning the exchange and she is not in her wave: stay for two or three autos, not one.
        # One auto after the burst left champions at 60-80% and no trade ever became a kill (g22-g24).
        ahead = sc.champ is not None and mi.hp_pct >= sc.champ.unit.hp * 100 + 10 and self.minions_near_champ(sc) < 3
        end = (burst + (1.8 if ahead else 0.7)) if burst >= t0 else (t0 + 2.5)
        if now < end:
            return False
        mi.trade_cooldown_until = max(mi.trade_cooldown_until, end + 4.0)
        if now < end + 1.2:
            mi.back_off(sc, now)
            mi.last_action = "trade: step back"
            return True
        mi.set_mode("farm", now)
        return False

    def hit_or_chase(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, reach: float = 0.0) -> bool:
        """Auto the champion when in range, otherwise walk onto them (orb-walk: move between autos).
        A trade does not chase: out of reach for 1.2 s it is over (walking after a ranged champion
        into her wave cost HP for nothing, g06)."""
        ch, d = sc.champ, sc.champ_dist or 9e9
        rng = (reach or VC.auto_range) + 60
        if d <= rng:
            self._in_reach_t = now
        elif mode == "trade" and now - max(getattr(self, "_in_reach_t", 0.0), mi.mode_since) > 1.2:
            mi.set_mode("farm", now)
            mi.trade_cooldown_until = now + 4.0
            mi.last_action = "trade: out of reach, back to farming"
            return False
        elif (mode == "all_in" and ch.unit.hp > 0.25 and not (mi.flash_in_ok and ch.unit.hp < 0.35)
              and now - max(getattr(self, "_in_reach_t", 0.0), mi.mode_since) > 2.5):
            # An all in she walks out of: 91 "all_in: chase" orders in g22 (Lee 61 in g25), walking after
            # healthy champions into their team. Under 25% the chase (and the Flash) goes on.
            mi.set_mode("farm", now)
            mi.trade_cooldown_until = now + 4.0
            mi.last_action, mi.last_action_t = "all in: out of reach 2.5 s, back to farming", now
            return False
        if d <= rng and mi.attack_ready(now, aspd):
            mi.attack(ch, now, f"{mode}: auto the champion")
            return True
        if mi.in_windup(now, aspd):
            return True
        # A fleeing champion under 25% is worth the Flash while I am healthy: two kills walked away at
        # 15-16% in g17 while Yasuo chased with Flash up.
        flash_ok = mi.flash_in_ok or (ch.unit.hp < 0.25 and mi.hp_pct >= 40)
        if mode == "all_in" and flash_ok and d > rng and d <= rng + 380:
            slot = mi.summoner_slot("flash", sc.ready)
            if slot:
                # Jev reads the kill as on and they are just out of reach: Flash onto them, auto next tick.
                mi.cast_summoner(slot, ch.unit.x, ch.unit.y)
                mi._ordered(now, f"{mode}: Flash in for the kill")
                mi.flash_in_ok = False
                return True
        x, y = ch.lead(now, 0.25)
        mi.move_screen(x, y, now, f"{mode}: stick to the champion" if d <= rng else f"{mode}: chase", every=0.12)
        return True

    def execute(self, mi: Micro, sc: Scene, tr, now: float) -> bool:
        """An enemy champion about to die within reach: one order that finishes it, whatever my own
        HP or mode (Fiddlesticks sat at 8% within 520 units while Yasuo backed off at 22%, g16).
        Base: ignite, else an auto in range."""
        d = sc.dist(tr)
        slot = mi.summoner_slot("ignite", sc.ready)
        if slot and d <= 600:
            mi.cast_summoner(slot, tr.unit.x, tr.unit.y)
            mi._ordered(now, "execute: ignite")
            return True
        if d <= VC.auto_range + 40:
            mi.attack(tr, now, "execute: auto the low champion")
            return True
        return False

    def ignite_if_kill(self, mi: Micro, sc: Scene, now: float, mode: str) -> bool:
        slot = mi.summoner_slot("ignite", sc.ready)
        if slot and mode == "all_in" and sc.champ.unit.hp < 0.35 and (sc.champ_dist or 9e9) <= 600:
            mi.cast_summoner(slot, sc.champ.unit.x, sc.champ.unit.y)
            mi._ordered(now, "ignite the champion")
            return True
        return False

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        """The standing behaviour between one-shot actions."""
        if mode == "back_off" and sc.champ is not None:
            mi.back_off(sc, now)
            return True
        if mode == "hold":
            return True
        if self.support and mode != "push":
            return mi.support_step(sc, now, aspd)
        return mi.farm_step(sc, now, aspd, push=(mode == "push" or pushing))

    def ready_words(self, sc: Scene, key: str) -> str:
        return "ready" if sc.ready.get(key) else "not ready"

    @staticmethod
    def modes(*names_texts: tuple[str, str]) -> list[Spec]:
        return [Spec(n, t, NONE, mode=n) for n, t in names_texts]


# ---------------------------------------------------------------------------------------
class QStacks:
    """Yasuo's Q stacks, counted from own casts: a Q with a unit in its line counts as a hit.
    Two stacks make the next Q the tornado; stacks expire after 6 s."""

    def __init__(self) -> None:
        self.stacks = 0
        self.last_gain = 0.0
        self.hud: bool | None = None   # the HUD's Q icon (whirlwind or blade), set every frame; wins when read

    def q3(self, now: float) -> bool:
        if self.hud is not None:
            return self.hud
        if self.stacks and now - self.last_gain > 6.0:
            self.stacks = 0
        return self.stacks >= 2

    def cast(self, hit: bool, now: float) -> None:
        if self.q3(now):
            self.stacks = 0
            return
        if hit:
            self.stacks += 1
            self.last_gain = now


def q_cast_time(aspd: float | None = None) -> float:
    """Steel Tempest's cast: 0.35 s, down to 0.175 s with bonus attack speed (wiki; base 0.697)."""
    if not aspd:
        return FAST.q_cast_s
    bonus = max(0.0, aspd / 0.697 - 1.0)
    return max(0.175, 0.35 / (1.0 + bonus))


def tornado_lead(d_units: float, aspd: float | None = None) -> float:
    """Seconds from the Q3 press to the tornado reaching `d_units`: the cast (shorter with attack
    speed) and the flight at 1200 units/s (wiki). The lead used 1500 and fell short on walkers."""
    return q_cast_time(aspd) + d_units / VC.q3_speed


def tornado_aim(sc: Scene, target, now: float, aspd: float | None = None) -> tuple[float, float]:
    """Where to throw the tornado: the line (1150 long, ~155 units either side counting hitboxes) that
    catches the most enemy champions at their led positions; the target's own line on a tie."""
    mx, my = sc.me_xy
    ppu = VC.px_per_unit
    led = []
    for t in (sc.champs or [target]):
        d = sc.dist(t)
        if d <= VC.q3_range * 0.95:
            led.append((t, t.lead(now, tornado_lead(d, aspd))))
    if not led:
        return target.lead(now, tornado_lead(sc.dist(target), aspd))
    best, best_n = None, -1
    for t, (ax, ay) in led:
        dx, dy = ax - mx, ay - my
        n = math.hypot(dx, dy) or 1.0
        hits = 0
        for _, (bx, by) in led:
            px, py = bx - mx, by - my
            along = (px * dx + py * dy) / n / ppu
            perp = abs(px * dy - py * dx) / n / ppu
            if 0 <= along <= VC.q3_range * 0.95 and perp <= 155:
                hits += 1
        if hits > best_n or (hits == best_n and t is target):
            best, best_n = (ax, ay), hits
    return best


def _line_hits(sc: Scene, x: float, y: float, rng_units: float, width_px: float = 45) -> bool:
    mx, my = sc.me_xy
    dx, dy = x - mx, y - my
    n = math.hypot(dx, dy) or 1.0
    rng = rng_units * VC.px_per_unit
    for tr in sc.minions + ([sc.champ] if sc.champ else []):
        ux, uy = tr.unit.x - mx, tr.unit.y - my
        along = (ux * dx + uy * dy) / n
        across = abs(ux * dy - uy * dx) / n
        if 0 < along <= rng and across < width_px:
            return True
    return False


def _hits_champ(sc: Scene, x: float, y: float, rng_units: float, width_px: float = 55) -> bool:
    """Would a line skillshot toward (x, y) also hit the enemy champion?"""
    mx, my = sc.me_xy
    dx, dy = x - mx, y - my
    n = math.hypot(dx, dy) or 1.0
    ux, uy = sc.champ.unit.x - mx, sc.champ.unit.y - my
    along = (ux * dx + uy * dy) / n
    return 0 < along <= (rng_units + 60) * VC.px_per_unit and abs(ux * dy - uy * dx) / n < width_px


class Yasuo(Kit):
    name = "Yasuo"
    champ_id = YASUO_ID
    default_role = "MIDDLE"
    skill_order = ["Q", "E", "Q", "W", "Q", "R", "Q", "E", "Q", "E", "R", "E", "E", "W", "W", "R", "W", "W"]
    items = ItemProfile(
        champion="Yasuo",
        core=["Berserker's Greaves", "Immortal Shieldbow", "Infinity Edge", "Blade of The Ruined King", "Death's Dance", "Guardian Angel"],
        starters=["Doran's Blade", "Health Potion"],
        boots=["Berserker's Greaves", "Plated Steelcaps", "Mercury's Treads"],
        crit_bonus=True,
        note="Yasuo doubles his crit chance",
    )

    def __init__(self, role: str = "") -> None:
        super().__init__(role)
        self.q = QStacks()
        self.tornado_at = 0.0

    # -- executors ---------------------------------------------------------------------
    def _q(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        was_q3 = self.q.q3(ctx.now)
        hit = _line_hits(ctx.sc, pt[0], pt[1], VC.q3_range if was_q3 else VC.q_range)
        ctx.mi.cast(1, *pt)
        self.q.cast(hit, ctx.now)
        if was_q3:
            self.tornado_at = ctx.now
        return "Q3 tornado" if was_q3 else "Q"

    def _e(self, ctx: Ctx, spec: Spec, tgt, pt, then_q: bool = False) -> str:
        ctx.mi.cast(3, tgt.unit.x, tgt.unit.y)
        tgt.e_marked_until = ctx.now + 10.0
        if then_q:
            time.sleep(FAST.eq_delay_s)
            ctx.mi.ctl.press(ctx.mi.kb.ability(1))  # Q during the dash: circular Q around Yasuo
            self.q.cast(True, ctx.now)
            return "E+Q"
        return "E"

    def _beyblade(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        champ = ctx.sc.champ
        ctx.mi.cast(3, tgt.unit.x, tgt.unit.y)
        tgt.e_marked_until = ctx.now + 10.0
        was_q3 = self.q.q3(ctx.now)
        ctx.mi.later(FAST.eq_delay_s, lambda: ctx.mi.ctl.press(ctx.mi.kb.ability(1)))
        ctx.mi.later(FAST.eq_delay_s + 0.06, lambda: ctx.flash_toward(champ.unit.x, champ.unit.y))
        self.q.cast(True, ctx.now)
        if was_q3:
            self.tornado_at = ctx.now + 0.15
        return "beyblade E+Q+Flash"

    def specs(self, ctx: Ctx) -> list[Spec]:
        sc, now = ctx.sc, ctx.now
        q3 = self.q.q3(now)
        out = self.modes(
            ("farm", "Keep farming: last-hit minions about to die, otherwise hold just behind the wave."),
            ("push", "Shove the wave: attack minions freely, ignore last-hit timing."),
            ("back_off", "Walk back toward my tower, away from the enemy champion."),
        ) + self.fight_modes(sc)
        not_marked = lambda k, u: getattr(u, "e_marked_until", 0.0) <= now  # E cannot reuse a unit for a while
        if sc.ready.get("Q"):
            if q3:
                out.append(Spec("Q", "Q3: throw the tornado along the chosen line: knocks up every enemy hit and enables R.",
                                POINT, who="enemy", range=VC.q3_range, run=self._q))
            else:
                out.append(Spec("Q", "Q: stab along the chosen line: damages every enemy hit and builds a stack toward the tornado.",
                                POINT, who="enemy", range=VC.q_range, run=self._q))
        # Purpose-named Q moves: the same spell, but Jev weighs "last hit with Q" or "tornado the
        # champion" far better than an abstract "Q along a line".
        if sc.ready.get("Q") and sc.killable_q:
            tq = sc.killable_q[0]
            out.append(Spec("Q_lasthit", "Q the minions: kills the low minion Q can finish now and builds a Q stack.", NONE,
                            run=lambda c, s, t, p, tq=tq: self._q(c, s, None, (tq.unit.x, tq.unit.y))))
        if sc.ready.get("Q") and sc.champ is not None and sc.champ_dist is not None:
            rng = VC.q3_range if q3 else VC.q_range
            if sc.champ_dist <= rng:
                ch = sc.champ
                name, text = (("Q3_tornado", "Throw the Q3 tornado at the enemy champion: knocks them up, then R can follow.")
                              if q3 else ("Q_poke", "Q the enemy champion: quick damage and a Q stack toward the tornado."))
                out.append(Spec(name, text, NONE, run=lambda c, s, t, p, ch=ch: self._q(c, s, None, (ch.unit.x, ch.unit.y))))
        if sc.ready.get("W"):
            out.append(Spec("W", "W: raise a wind wall in the chosen direction: it blocks enemy projectiles.", POINT, range=400,
                            run=lambda c, s, t, p: (c.mi.cast(2, *p), "W wind wall")[1]))
        # Dashes are gated: with the enemy champion close and the fight read unfavourable, dashing
        # forward is how the lane gets lost (5 E's in game 2 took no HP off the enemy, then a death).
        safe_to_dash = (sc.champ is None or (sc.champ_dist or 9e9) > 700
                        or float(ctx.plan.get("fight_favorable", 0.5)) >= 0.5)
        if sc.ready.get("E") and (sc.minions or sc.champ is not None) and safe_to_dash:
            out.append(Spec("E", "E: dash through the chosen enemy unit (not one dashed through in the last few seconds).",
                            UNIT, who="enemy", range=VC.e_range, accept=not_marked, run=self._e))
            if sc.ready.get("Q"):
                out.append(Spec("E_then_Q", "E through the chosen enemy unit and Q during the dash (circle Q: hits everything around me).",
                                UNIT, who="enemy", range=VC.e_range, accept=not_marked,
                                run=lambda c, s, t, p: self._e(c, s, t, p, then_q=True)))
        if sc.ready.get("E") and sc.ready.get("Q") and ctx.flash_slot() and sc.champ is not None and safe_to_dash:
            out.append(Spec("beyblade", "E through the chosen enemy unit, Q during the dash, and Flash onto the enemy champion "
                            "during the Q: the circle Q (a knock-up with the tornado) lands on them from out of range.",
                            UNIT, who="enemy", range=VC.e_range, accept=not_marked, run=self._beyblade))
        if sc.r_lit and sc.champ is not None:
            out.append(Spec("R", "R: Last Breath onto the airborne enemy champion: big damage while they are knocked up.",
                            UNIT, who="enemy_champion", range=VC.r_range,
                            run=lambda c, s, t, p: (c.mi.cast(4, t.unit.x, t.unit.y), "R Last Breath")[1]))
        return out

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        q3 = self.q.q3(now)
        return {
            "Q": ("Q3 tornado ready" if q3 else "ready") if sc.ready.get("Q") else "on cooldown",
            "W": self.ready_words(sc, "W"), "E": self.ready_words(sc, "E"),
            "R": "castable (enemy airborne)" if sc.r_lit else "not castable",
        }

    def e_minion_damage(self, e_rank: int, ad: float, level: int) -> float:
        if not e_rank:
            return 0.0
        bonus = max(0.0, ad - (60.0 + 3.0 * (level - 1)))  # base AD 60 + 3 per level (approximate)
        return 0.85 * (FAST.e_base[min(e_rank, 5) - 1] + FAST.e_bonus_ad * bonus)

    @staticmethod
    def _landing(sc: Scene, tr) -> tuple[float, float]:
        """Where E through `tr` ends: a fixed dash length from me, through the unit."""
        mx, my = sc.me_xy
        dx, dy = tr.unit.x - mx, tr.unit.y - my
        n = math.hypot(dx, dy) or 1.0
        r = VC.e_range * VC.px_per_unit
        return mx + dx / n * r, my + dy / n * r

    @staticmethod
    def _dash_safe(mi: Micro, sc: Scene, land: tuple[float, float]) -> bool:
        """A farm dash that does not end next to a healthier champion, alone in their wave, or near
        their tower."""
        if not mi.dash_ok:
            return False
        ch = sc.champ
        if ch is not None:
            if mi.hp_pct < 35:
                return False
            if (math.hypot(ch.unit.x - land[0], ch.unit.y - land[1]) / VC.px_per_unit < 500
                    and mi.hp_pct < ch.unit.hp * 100 + 10):
                return False
        crowd = sum(1 for t in sc.minions if math.hypot(t.unit.x - land[0], t.unit.y - land[1]) / VC.px_per_unit < 400)
        return not (sc.allies == 0 and crowd >= 3 and mi.hp_pct < 70)

    def _eq_after(self, mi: Micro, sc: Scene, land: tuple[float, float], now: float, fight: bool, skip=None) -> bool:
        """Q during the dash (the circle around the landing spot) when it lands on something worth
        it: the champion in a fight; in lane, another minion (a stack) and her only when I am ahead.
        The tornado is kept for her. E+Q happened only inside trades before (0 in g28-g29)."""
        if not sc.ready.get("Q"):
            return False
        r = FAST.eq_radius * VC.px_per_unit
        inside = lambda u: math.hypot(u.x - land[0], u.y - land[1]) <= r
        champ_in = sc.champ is not None and inside(sc.champ.unit)
        q3 = self.q.q3(now)
        if fight:
            if not champ_in:
                return False  # the thrust after landing reaches her; the circle would not
        else:
            if q3 and not champ_in:
                return False
            if champ_in and mi.hp_pct < sc.champ.unit.hp * 100 + 10:
                return False  # hitting her turns her wave on me: only when I win the exchange
            if not champ_in and not any(inside(t.unit) for t in sc.minions if t is not skip):
                return False
        mi.later(FAST.eq_delay_s, lambda: mi.ctl.press(mi.kb.ability(1)))
        self.q.cast(True, now)
        if q3:
            self.tornado_at = now + 0.15
        if champ_in:
            self.burst_at = now
        return True

    def _e_lasthit(self, mi: Micro, sc: Scene, now: float, aspd: float) -> bool:
        """E through a minion the dash kills, when an auto cannot take it now (on cooldown, or it
        needs a walk): Yasuo's usual last hit, and with Q up the circle Q in the dash stacks Q."""
        if not sc.ready.get("E") or not sc.killable_e or not mi._can_order(now) or mi.in_windup(now, aspd):
            return False
        auto_now = mi.attack_ready(now, aspd)
        for tr in sc.killable_e:
            if auto_now and tr in sc.killable_auto and sc.dist(tr) <= VC.auto_range + 40:
                continue  # the auto takes it
            land = self._landing(sc, tr)
            if not self._dash_safe(mi, sc, land):
                continue
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            mi.attacked_ids[tr.id] = now
            eq = self._eq_after(mi, sc, land, now, fight=False, skip=tr)
            mi._ordered(now, "E+Q last hit" if eq else "E last hit")
            mi.lh_pending.append((now, f"E-{getattr(tr, 'role', '') or '?'}", tr.unit.hp))
            return True
        return False

    def plan_step(self, mi: Micro, sc: Scene, now: float, aspd: float, pushing: bool, poke: bool = False) -> bool:
        """The lane planner's best option this tick, executed (laneplan.py)."""
        from jev.laneplan import LanePlanner

        if mi.in_windup(now, aspd):
            return True  # a move now cancels the auto
        if not mi._can_order(now):
            return True
        if getattr(self, "_planner", None) is None:
            self._planner = LanePlanner(self)
        if poke:
            sc.lane = dict(sc.lane, aggression=max(1.3, float(sc.lane.get("aggression", 1.0))))
        best, top = self._planner.choose(sc, mi, now, pushing)
        mi.plan_log.append({"t": round(now, 2), "pick": best.brief(), "also": [o.brief() for o in top[1:]]})
        k, tr = best.kind, best.target
        role = (getattr(tr, "role", "") or "?") if tr is not None else ""
        if k == "auto":
            if best.p >= 0.5:
                mi.attack(tr, now, f"last hit ({int(tr.unit.hp * 100)}%)")
                mi.lh_pending.append((now, f"auto-{role}", tr.unit.hp, best.z))
            else:
                mi.attack(tr, now, "push: attack the wave")
            return True
        if k == "q":
            was_q3 = self.q.q3(now)
            rng = VC.q3_range * 0.8 if was_q3 else VC.q_range
            for u in self._planner._line_units(sc, tr.unit.x, tr.unit.y, rng):
                u.hits_expected.append((now + FAST.lasthit_lead_s + FAST.q_cast_s, sc.q_dmg / 0.88))
            mi.cast(1, tr.unit.x, tr.unit.y)
            self.q.cast(True, now)
            mi.attacked_ids[tr.id] = now
            if best.p >= 0.5:
                mi.lh_pending.append((now, f"Q-{role}", tr.unit.hp, best.z))
            mi._ordered(now, ("Q3 the wave" if was_q3 else "Q last hit") if best.p >= 0.5 else ("push: Q3 the wave" if was_q3 else "Q: stack on the wave"))
            return True
        if k in ("e", "eq"):
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            mi.attacked_ids[tr.id] = now
            if k == "eq":
                mi.later(FAST.eq_delay_s, lambda: mi.ctl.press(mi.kb.ability(1)))
                self.q.cast(True, now)
            if best.p >= 0.5:
                mi.lh_pending.append((now, f"E-{role}", tr.unit.hp, best.z))
            if getattr(mi, "learner", None) is not None:
                mi.learner.dashed(now, mi.hp_pct)
            label = "E+Q" if k == "eq" else "E"
            mi._ordered(now, f"{label} last hit" if best.p >= 0.5 else f"{label} through a minion ({best.why[:40]})")
            return True
        if k in ("auto_champ", "q_champ", "q3_champ", "eq_champ") and getattr(mi, "learner", None) is not None:
            her_wave = sum(1 for t in sc.minions if math.hypot(t.unit.x - tr.unit.x, t.unit.y - tr.unit.y) <= 500 * VC.px_per_unit)
            mi.learner.hit_her(now, mi.hp_pct, tr.unit.hp, her_wave)
        if k == "auto_champ":
            mi.attack(tr, now, "plan: auto the champion")
            self.burst_at = now
            return True
        if k in ("q_champ", "q3_champ"):
            q3 = k == "q3_champ"
            d = sc.champ_dist or 0.0
            x, y = tornado_aim(sc, tr, now, aspd) if q3 else tr.lead(now, q_cast_time(aspd) + 0.05)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            self.burst_at = now
            if q3:
                self.tornado_at = now
            mi._ordered(now, "plan: Q3 tornado at the champion" if q3 else "plan: Q the champion")
            return True
        if k == "eq_champ":
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            mi.later(FAST.eq_delay_s, lambda: mi.ctl.press(mi.kb.ability(1)))
            self.q.cast(True, now)
            self.burst_at = now
            mi._ordered(now, "plan: E+Q onto the champion")
            return True
        if k == "move":
            mi.move_screen(best.point[0], best.point[1], now, f"plan: stand ({best.why[:48]})", every=0.3)
            return True
        return True  # hold

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if FAST.lane_planner and mode in ("farm", "push") and sc.minions:
            return self.plan_step(mi, sc, now, aspd, pushing or mode == "push")
        if FAST.lane_planner and mode == "back_off" and sc.minions and sc.champ is not None and mi.hp_pct >= 30:
            # Backing off is keeping out of her reach, not walking away from the wave: Jev's back_off was
            # 44% of fight reads in g29 and every one of them cost the last hits. The planner, cautious:
            # her reach counts double with a margin, no hits on her, last hits where she cannot reach.
            sc.lane = dict(sc.lane, aggression=0.0, cautious=True)
            return self.plan_step(mi, sc, now, aspd, False)
        if (pushing and mode in ("farm", "push") and sc.champ is None and sc.ready.get("Q") and not sc.killable_q
                and not sc.killable_auto and mi._can_order(now)):  # a last hit first, the wave after
            # Pushing: Q into the wave on cooldown (the tornado too: it hits the whole line).
            inq = [t for t in sc.minions if sc.dist(t) <= (VC.q3_range * 0.8 if self.q.q3(now) else VC.q_range)]
            if inq:
                tq = min(inq, key=lambda t: t.unit.hp)
                was_q3 = self.q.q3(now)
                mi.cast(1, tq.unit.x, tq.unit.y)
                self.q.cast(True, now)
                mi._ordered(now, "push: Q3 the wave" if was_q3 else "push: Q the wave")
                return True
        # Farming Yasuo uses Q on cooldown on minions it can kill (standard play: CS and Q stacks).
        if mode in ("farm", "push") and sc.ready.get("Q") and sc.killable_q and mi._can_order(now):
            tq = sc.killable_q[0]
            was_q3 = self.q.q3(now)
            if was_q3 and sc.champ is not None and (sc.champ_dist or 9e9) <= VC.q3_range:
                return super().continuous(mi, sc, now, aspd, mode, pushing)  # the tornado is kept for her
            if sc.champ is not None and _hits_champ(sc, tq.unit.x, tq.unit.y, VC.q3_range if was_q3 else VC.q_range) \
                    and len(sc.minions) >= 3 and sc.champ.unit.hp > 0.3:
                # The Q would also hit their champion, and hitting a champion turns her whole wave on
                # me: a level-2 Q last hit through Kayle cost 88% -> 59% in two seconds (g16). Auto it.
                return super().continuous(mi, sc, now, aspd, mode, pushing)
            hit = _line_hits(sc, tq.unit.x, tq.unit.y, VC.q3_range if was_q3 else VC.q_range)
            mi.attacked_ids[tq.id] = now
            mi.cast(1, tq.unit.x, tq.unit.y)
            self.q.cast(hit, now)
            mi._ordered(now, "Q last hit")
            mi.lh_pending.append((now, f"Q-{getattr(tq, 'role', '') or '?'}", tq.unit.hp))
            mi.last_exec = {"name": "Q last hit", "what": "Q last hit", "ts": now, "target": mi._pt(tq.unit.x, tq.unit.y), "point": None}
            return True
        if mode in ("farm", "push") and self._e_lasthit(mi, sc, now, aspd):
            return True
        return super().continuous(mi, sc, now, aspd, mode, pushing)

    def poke(self, mi: Micro, sc: Scene, now: float, aspd: float) -> bool:
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        if FAST.lane_planner and (sc.minions or ch is not None):
            return self.plan_step(mi, sc, now, aspd, False, poke=True)
        if mi._can_order(now) and rdy.get("Q"):
            q3 = self.q.q3(now)
            if (q3 and d <= VC.q3_range * 0.9) or (not q3 and d <= VC.q_range + 20):
                x, y = tornado_aim(sc, ch, now, aspd) if q3 else ch.lead(now, q_cast_time(aspd) + 0.05)
                mi.cast(1, x, y)
                self.q.cast(True, now)
                if q3:
                    self.tornado_at = now
                mi._ordered(now, "poke: Q3 tornado" if q3 else "poke: Q the champion")
                return True
        return self.poke_auto(mi, sc, now, aspd) or self.continuous(mi, sc, now, aspd, "farm", False)

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        """Yasuo's combo, one order per tick: R on an airborne target; Q3 tornado from range (led
        onto where they walk); E onto them with Q in the dash (EQ, a knock-up with Q3); Q in
        melee range; ignite a kill; E through a minion toward them to close the gap; autos."""
        if mode == "poke":
            return self.poke(mi, sc, now, aspd)
        if mode == "trade" and self.trade_over(mi, sc, now):
            return True
        if not mi._can_order(now):
            return True
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        q3 = self.q.q3(now)
        if sc.r_lit and d <= VC.r_range:
            mi.cast(4, ch.unit.x, ch.unit.y)
            mi._ordered(now, f"{mode}: R Last Breath")
            self.r_at = now
            self.burst_at = now
            return True
        if mi.in_windup(now, aspd):
            return True  # auto, Q, auto: a spell in the swing throws the auto away (the knock-up's R above is the exception)
        if mode == "all_in" and self._beyblade_in(mi, sc, now, q3):
            return True
        if rdy.get("Q") and q3 and d <= VC.q3_range * 0.92 and d > VC.e_range:
            x, y = tornado_aim(sc, ch, now, aspd)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            self.tornado_at = now
            mi._ordered(now, f"{mode}: Q3 tornado")
            return True
        # Her wave matters for a trade (its aggro outlasts the exchange), not for a committed all in
        # while I am healthy: holding E there turned all ins into auto chases she walked out of (g16-g17).
        crowded = self.minions_near_champ(sc) >= 3 and not (mode == "all_in" and (ch.unit.hp < 0.35 or mi.hp_pct >= 50))
        if crowded and mode == "trade" and not (rdy.get("Q") and d <= VC.q_range + 30) and not (q3 and rdy.get("Q")):
            mi.set_mode("farm", now)  # she stands in her wave and nothing reaches her from here: no trade
            mi.trade_cooldown_until = now + 4.0
            mi.last_action = "trade: she is in her wave, back to farming"
            return False
        # E through her lands 475 from where I start: from under 230 units that is 245+ past her, out of
        # the E+Q circle (215) and of auto range. Closer than that, Q and autos do it.
        if rdy.get("E") and VC.eq_min <= d <= VC.e_range and ch.e_marked_until <= now and not crowded:
            mi.cast(3, ch.unit.x, ch.unit.y)
            ch.e_marked_until = now + 10.0
            if rdy.get("Q"):
                was_q3 = q3
                mi.later(FAST.eq_delay_s, lambda: mi.ctl.press(mi.kb.ability(1)))
                self.q.cast(True, now)
                if was_q3:
                    self.tornado_at = now + 0.15
                self.burst_at = now
                mi._ordered(now, f"{mode}: E+Q onto the champion" + (" (tornado)" if was_q3 else ""))
            else:
                mi._ordered(now, f"{mode}: E onto the champion")
            return True
        if rdy.get("Q") and d <= VC.q_range + 30:
            x, y = tornado_aim(sc, ch, now, aspd) if q3 else ch.lead(now, q_cast_time(aspd) + 0.05)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            if q3:
                self.tornado_at = now
            self.burst_at = now
            mi._ordered(now, f"{mode}: Q the champion")
            return True
        if (rdy.get("W") and 350 < d <= 1000 and getattr(mi, "hp_lost", 0.0) >= 10
                and now - getattr(self, "_wall_at", 0.0) > 5.0):
            # Losing HP fast to a champion out of melee range: their damage is projectiles, wall it.
            mi.cast(2, ch.unit.x, ch.unit.y)
            self._wall_at = now
            mi._ordered(now, f"{mode}: W wind wall toward the champion")
            return True
        if self.ignite_if_kill(mi, sc, now, mode):
            return True
        if rdy.get("E") and d > VC.auto_range + 120 and sc.dash_options and (mode == "all_in" or rdy.get("Q")) and not crowded:
            tr = sc.dash_options[0][0]
            land = self._landing(sc, tr)
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            eq = self._eq_after(mi, sc, land, now, fight=True)
            mi._ordered(now, f"{mode}: E{'+Q' if eq else ''} through a minion toward the champion")
            return True
        return self.hit_or_chase(mi, sc, now, aspd, mode)

    def escape(self, mi: Micro, sc: Scene, now: float, home: tuple[float, float]) -> bool:
        """E through a minion whose far side is toward home, when an enemy champion is near and
        I am hurt (the dash outruns a chase; it is Yasuo's only escape before Flash)."""
        if not sc.ready.get("E") or sc.champ is None or (sc.champ_dist or 9e9) > 900 or mi.hp_pct > 60:
            return False
        mx, my = sc.me_xy
        best, gain = None, 120.0
        for tr in sc.minions:
            if tr.e_marked_until > now or sc.dist(tr) > VC.e_range:
                continue
            dx, dy = tr.unit.x - mx, tr.unit.y - my
            n = math.hypot(dx, dy) or 1.0
            along = (dx / n * home[0] + dy / n * home[1]) * VC.e_range  # units gained toward home
            if along > gain:
                best, gain = tr, along
        if best is None:
            return False
        mi.cast(3, best.unit.x, best.unit.y)
        best.e_marked_until = now + 10.0
        mi._ordered(now, "escape: E through a minion toward home")
        return True

    r_rank = 0  # set by the loop from the API (R is only lit on screen while someone is airborne)

    def r_up(self, now: float) -> bool:
        """R learned and off cooldown, from our own casts (80 / 55 / 30 s by rank)."""
        cd = {1: 80.0, 2: 55.0, 3: 30.0}.get(self.r_rank, 80.0)
        return self.r_rank >= 1 and now - getattr(self, "r_at", -1e9) > cd

    def _beyblade_in(self, mi: Micro, sc: Scene, now: float, q3: bool) -> bool:
        """E through a minion toward her, Q3 buffered in the dash, Flash onto her: the circle knock-up
        lands where the Flash does (wiki), R follows on the reflex. Only when the kill is on (Jev's
        Flash-in, or her under 35% with me healthy and R up): otherwise the tornado from range, which
        costs no Flash, opens."""
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        slot = mi.summoner_slot("flash", rdy)
        if not (q3 and rdy.get("E") and rdy.get("Q") and slot and self.r_up(now) and d > VC.e_range):
            return False
        if not (mi.flash_in_ok or (ch.unit.hp < 0.35 and mi.hp_pct >= 60)):
            return False
        best = None
        for m in sc.minions:
            if m.e_marked_until > now or sc.dist(m) > VC.e_range:
                continue
            land = self._landing(sc, m)
            dl = math.hypot(land[0] - ch.unit.x, land[1] - ch.unit.y) / VC.px_per_unit
            if 215 < dl <= 400 + 150 and (best is None or dl < best[0]):  # Flash (400) then the circle (215) reaches her
                best = (dl, m)
        if best is None:
            return False
        m = best[1]
        tx, ty = ch.lead(now, 0.3)
        mi.cast(3, m.unit.x, m.unit.y)
        m.e_marked_until = now + 10.0
        mi.later(FAST.eq_delay_s, lambda: mi.ctl.press(mi.kb.ability(1)))
        mi.later(FAST.eq_delay_s + 0.06, lambda: mi.cast_summoner(slot, tx, ty))
        self.q.cast(True, now)
        self.tornado_at = now + 0.2
        self.burst_at = now
        mi.flash_in_ok = False
        mi._ordered(now, "all_in: beyblade (E+Q3+Flash onto her)")
        return True

    def trade_window(self, mi: Micro, sc: Scene, now: float, level_diff: int) -> str | None:
        """EQ (or the Q3 tornado) is up, the champion is in dash or tornado reach, I am at least
        as healthy and as high level: trade. With R up and a knock-up in hand, a champion under
        55% is an all in (the knock-up into R is Yasuo's kill combo; R went unused in g09-g13)."""
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        if now - getattr(self, "_last_window", 0.0) < 8.0 or level_diff < 0:
            return None
        knockup = rdy.get("Q") and self.q.q3(now) and d <= VC.q3_range * 0.85
        if (self.r_up(now) and knockup and ch.unit.hp < 0.55 and mi.hp_pct >= 45 and not (sc.enemy_champs >= 2 and not sc.ally_champs)
                and self.minions_near_champ(sc) < 4):
            self._last_window = now
            return "all_in"
        # Her burst on cooldown (enemies.py): the window laners trade in, a few points behind or not.
        slack = (15 if sc.lane.get("her_spells_down") else 5) + (5 if sc.lane.get("shield_ready") else 0)
        if mi.hp_pct < 50 or mi.hp_pct < ch.unit.hp * 100 - slack:
            return None
        if sc.enemy_champs >= 2 and not sc.ally_champs:
            return None  # two of them in view and none of us: not a trade
        if self.minions_near_champ(sc) >= 3 and ch.unit.hp > 0.35:
            return None  # trading into her full wave: Yasuo took the minions' aggro and lost 78% -> 59% (g04)
        # (E reach plus a step: opened at up to 625 units, the E was out of range and the trade became
        # a walk after her, "trade: chase", g29.)
        eq = rdy.get("E") and rdy.get("Q") and VC.eq_min <= d <= VC.e_range + 50
        tornado = rdy.get("Q") and self.q.q3(now) and d <= VC.q3_range * 0.85
        if not (eq or tornado):
            return None
        self._last_window = now
        return "all_in" if (ch.unit.hp < 0.45 and mi.hp_pct > 60) else "trade"

    def execute(self, mi: Micro, sc: Scene, tr, now: float) -> bool:
        d = sc.dist(tr)
        q3 = self.q.q3(now)
        if sc.ready.get("Q") and d <= (VC.q3_range * 0.9 if q3 else VC.q_range + 30):
            x, y = tr.lead(now, tornado_lead(d, sc.aspd) if q3 else q_cast_time(sc.aspd) + 0.05)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            if q3:
                self.tornado_at = now
            mi._ordered(now, "execute: Q the low champion")
            return True
        if sc.ready.get("E") and d <= VC.e_range and tr.e_marked_until <= now:
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            mi._ordered(now, "execute: E onto the low champion")
            return True
        return super().execute(mi, sc, tr, now)

    def reflex(self, mi: Micro, sc: Scene, now: float, plan: dict) -> bool:
        """R the moment our own tornado lifts the target, if the fight read is favourable; the wind
        wall whenever a champion out of melee range is taking HP off us fast (any mode: Kayle's autos
        burst Yasuo from 75% to 41% in two seconds while he farmed, g13)."""
        allies_knock = (sc.r_lit and sc.champ is not None and len(sc.ally_champs) >= 1 and mi.hp_pct >= 40
                        and sc.enemy_champs <= len(sc.ally_champs) + 1 and (sc.champ_dist or 9e9) <= VC.r_range
                        and (sc.champ.unit.hp < 0.7 or len(sc.ally_champs) >= sc.enemy_champs))
        if sc.r_lit and sc.champ is not None and ((now - self.tornado_at < FAST.r_watch_s and plan.get("fight_favorable", 0.5) >= 0.45)
                                                  or allies_knock):
            # (R lights for an ally's knock-up too: a team fight we are even in is Yasuo's best ult.)
            mi.cast(4, sc.champ.unit.x, sc.champ.unit.y)
            mi._ordered(now, "R reflex after tornado" if now - self.tornado_at < FAST.r_watch_s else "R on an ally's knock-up")
            self.r_at = now
            self.tornado_at = 0.0
            if sc.champ.unit.hp < 0.65 and mi.hp_pct >= 35:
                mi.set_mode("all_in", now)  # knocked up and R'd: finish them
            return True
        d = sc.champ_dist or 9e9
        if (sc.ready.get("Q") and self.q.q3(now) and sc.champ is not None and 450 < d <= VC.q3_range * 0.85
                and mi.hp_pct >= 55 and mi.hp_pct >= sc.champ.unit.hp * 100 - 15  # not when behind: a poke from 63% on a
                # full Lux drew her combo and cost 43% (g18)
                and not getattr(mi, "near_enemy_tower", False) and mi.mode not in ("back_off",)
                and now - getattr(self, "_poke_at", 0.0) > 2.0):
            # The tornado is for the champion, not a minion: it passes through the wave, knocks up, and
            # with R ready turns into the kill combo (the reflex above). Nothing chose poke without Jev.
            x, y = sc.champ.lead(now, tornado_lead(d))
            mi.cast(1, x, y)
            self.q.cast(True, now)
            self.tornado_at = self._poke_at = now
            mi._ordered(now, "poke: Q3 tornado at the champion")
            return True
        if (sc.ready.get("W") and sc.champ is not None and 350 < d <= 1000 and getattr(mi, "hp_lost", 0.0) >= 10
                and now - getattr(self, "_wall_at", 0.0) > 5.0):
            mi.cast(2, sc.champ.unit.x, sc.champ.unit.y)
            self._wall_at = now
            mi._ordered(now, "W wind wall toward the champion")
            return True
        return False


# ---------------------------------------------------------------------------------------
class Thresh(Kit):
    name = "Thresh"
    champ_id = THRESH_ID
    default_role = "UTILITY"
    skill_order = ["Q", "E", "W", "Q", "Q", "R", "Q", "E", "Q", "E", "R", "E", "E", "W", "W", "R", "W", "W"]
    items = ItemProfile(
        champion="Thresh",
        core=["Plated Steelcaps", "Locket of the Iron Solari", "Knight's Vow", "Zeke's Convergence", "Thornmail", "Frozen Heart"],
        starters=["World Atlas", "Health Potion"],
        boots=["Plated Steelcaps", "Mercury's Treads", "Boots of Swiftness", "Ionian Boots of Lucidity"],
        support=True,
        note="Thresh is a tank support who protects his carry",
    )
    HOOK_RANGE, FLAY_RANGE, LANTERN_RANGE = 1050.0, 450.0, 950.0

    def __init__(self, role: str = "") -> None:
        super().__init__(role)
        self.hook_at = 0.0

    def hooked(self, sc: Scene, now: float) -> bool:
        # A landed hook re-lights Q (the recast) for about 1.5 s.
        return bool(sc.ready.get("Q")) and 0.25 < now - self.hook_at < 1.8

    def _hook(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        ctx.mi.cast(1, *pt)
        self.hook_at = ctx.now
        return "Q hook"

    def _flash_hook(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        ctx.mi.cast(1, tgt.unit.x, tgt.unit.y)
        self.hook_at = ctx.now
        ctx.mi.later(0.12, lambda: ctx.flash_toward(tgt.unit.x, tgt.unit.y))
        return "flash hook"

    def _fly_flay(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        ctx.mi.ctl.press(ctx.mi.kb.ability(1))
        champ = ctx.sc.champ

        def pull() -> None:
            # After the fly Thresh sits beside the target: cast E away from them to sweep them back.
            mx, my = ctx.sc.me_xy
            dx, dy = champ.unit.x - mx, champ.unit.y - my
            n = math.hypot(dx, dy) or 1.0
            ctx.mi.cast(3, mx - dx / n * 150, my - dy / n * 150)

        ctx.mi.later(0.45, pull)
        return "Q2 fly + flay pull"

    def _flay(self, ctx: Ctx, tgt, pull: bool) -> str:
        mx, my = ctx.sc.me_xy
        dx, dy = tgt.unit.x - mx, tgt.unit.y - my
        n = math.hypot(dx, dy) or 1.0
        x, y = (mx - dx / n * 150, my - dy / n * 150) if pull else (tgt.unit.x, tgt.unit.y)
        ctx.mi.cast(3, x, y)
        return "flay pull" if pull else "flay push"

    def specs(self, ctx: Ctx) -> list[Spec]:
        sc, now = ctx.sc, ctx.now
        out = self.modes(
            ("hold_with_carry", "Stay beside my carry, zone the enemy, and leave last hits to the carry."),
            ("push", "Help shove the wave: attack minions."),
            ("back_off", "Walk back toward my tower, away from the enemy champion."),
        )
        if self.hooked(sc, now):
            out.append(Spec("Q2_fly", "Q again: fly to the hooked enemy and engage (only if my carry can follow).", NONE,
                            run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.ability(1)), "Q2 fly")[1]))
            if sc.ready.get("E") and sc.champ is not None:
                out.append(Spec("fly_then_pull", "Q again to fly to the hooked enemy, then E behind me on landing to drag them "
                                "back toward my carry.", NONE, run=self._fly_flay))
        elif sc.ready.get("Q"):
            out.append(Spec("Q", "Q: throw the hook along the chosen line: the first enemy hit is stunned and pulled toward me.",
                            POINT, who="enemy", range=self.HOOK_RANGE, run=self._hook))
            if ctx.flash_slot() and sc.champ is not None:
                out.append(Spec("flash_hook", "Throw the hook at the chosen enemy champion and Flash toward them during the wind-up, "
                                "so the hook starts closer and they cannot walk out of it.",
                                UNIT, who="enemy_champion", range=self.HOOK_RANGE + 400, run=self._flash_hook))
        if sc.ready.get("W"):
            out.append(Spec("W", "W: throw the lantern to the chosen spot: an ally near it is shielded and can click it to come to me.",
                            POINT, who="ally_champion", range=self.LANTERN_RANGE,
                            run=lambda c, s, t, p: (c.mi.cast(2, *p), "W lantern")[1]))
        if sc.ready.get("E"):
            out.append(Spec("E", "E: sweep the chain in the chosen direction; enemies hit are knocked the way it sweeps.",
                            POINT, range=self.FLAY_RANGE, run=lambda c, s, t, p: (c.mi.cast(3, *p), "E flay")[1]))
            if sc.champ is not None:
                out.append(Spec("E_pull", "E behind me: pull the chosen enemy champion toward me and my carry (engage).",
                                UNIT, who="enemy_champion", range=self.FLAY_RANGE, run=lambda c, s, t, p: self._flay(c, t, True)))
                out.append(Spec("E_push", "E at them: knock the chosen enemy champion away from me (peel, disengage).",
                                UNIT, who="enemy_champion", range=self.FLAY_RANGE, run=lambda c, s, t, p: self._flay(c, t, False)))
        if sc.ready.get("R"):
            out.append(Spec("R", "R: The Box: walls around me that slow and damage enemies who cross them.", NONE,
                            run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.ability(4)), "R box")[1]))
        return out

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        return {
            "Q_hook": ("hook landed, can fly to target" if self.hooked(sc, now) else self.ready_words(sc, "Q")),
            "W_lantern": self.ready_words(sc, "W"), "E_flay": self.ready_words(sc, "E"),
            "R_box": self.ready_words(sc, "R"),
        }

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if mode == "hold_with_carry":
            mode = "farm"
        return super().continuous(mi, sc, now, aspd, mode, pushing)


# ---------------------------------------------------------------------------------------
class Soraka(Kit):
    name = "Soraka"
    champ_id = 16
    default_role = "UTILITY"
    skill_order = ["Q", "W", "E", "Q", "Q", "R", "Q", "W", "Q", "W", "R", "W", "W", "E", "E", "R", "E", "E"]
    items = ItemProfile(
        champion="Soraka",
        core=["Ionian Boots of Lucidity", "Moonstone Renewer", "Redemption", "Mikael's Blessing", "Echoes of Helia", "Dawncore"],
        starters=["World Atlas", "Health Potion"],
        boots=["Ionian Boots of Lucidity", "Boots of Swiftness", "Plated Steelcaps", "Mercury's Treads"],
        support=True,
        note="Soraka is a healer support who keeps her carry alive",
    )

    def specs(self, ctx: Ctx) -> list[Spec]:
        sc = ctx.sc
        out = self.modes(
            ("hold_with_carry", "Stay beside my carry, out of the enemy's reach, and leave last hits to the carry."),
            ("push", "Help shove the wave: attack minions."),
            ("back_off", "Walk back toward my tower, away from the enemy."),
        )
        if sc.ready.get("Q"):
            out.append(Spec("Q", "Q Starcall: a star falls at the chosen spot, damaging and slowing enemies there; hitting an enemy champion heals me.",
                            POINT, who="enemy", range=800, run=lambda c, s, t, p: (c.mi.cast(1, *p), "Q starcall")[1]))
        if sc.ready.get("W") and ctx.ally_units:
            out.append(Spec("W", "W Astral Infusion: heal the chosen allied champion (costs some of my own health).",
                            UNIT, who="ally_champion", range=550, run=lambda c, s, t, p: (c.mi.cast(2, t.x, t.y), "W heal")[1]))
        if sc.ready.get("E"):
            out.append(Spec("E", "E Equinox: a zone at the chosen spot that silences enemies inside and roots them if they stay.",
                            POINT, who="enemy", range=925, run=lambda c, s, t, p: (c.mi.cast(3, *p), "E equinox")[1]))
        if sc.ready.get("R"):
            out.append(Spec("R", "R Wish: heal every allied champion anywhere on the map (save it for when an ally is about to die).",
                            NONE, run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.ability(4)), "R wish")[1]))
        return out

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        carry = min(sc.ally_champs, key=lambda u: math.hypot(u.x - sc.me_xy[0], u.y - sc.me_xy[1])) if sc.ally_champs else None
        return {"Q": self.ready_words(sc, "Q"), "W_heal": self.ready_words(sc, "W"), "E_silence": self.ready_words(sc, "E"),
                "R_wish": self.ready_words(sc, "R"), "carry_hp_percent": int(carry.hp * 100) if carry else None}

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if mode == "hold_with_carry":
            mode = "farm"
        return super().continuous(mi, sc, now, aspd, mode, pushing)


class Generic(Kit):
    """Any champion without a hand-written kit: Q W E R as casts at a chosen spot (the target when
    aiming at a unit) with the game's own ability names; Jev knows what they do."""

    default_role = ""
    skill_order = ["Q", "W", "E", "Q", "Q", "R", "Q", "W", "Q", "W", "R", "W", "W", "E", "E", "R", "E", "E"]

    def __init__(self, champion: str, role: str = "", ability_names: dict | None = None) -> None:
        self.name = champion or "champion"
        super().__init__(role or "MIDDLE")
        self.ability_names = ability_names or {}
        self.items = ItemProfile(champion=self.name, core=[], starters=["Health Potion"] if not self.support else ["World Atlas", "Health Potion"],
                                 boots=["Plated Steelcaps", "Mercury's Treads", "Ionian Boots of Lucidity"], support=self.support)

    def specs(self, ctx: Ctx) -> list[Spec]:
        sc = ctx.sc
        if self.support:
            out = self.modes(("hold_with_carry", "Stay beside my carry and leave last hits to the carry."),
                             ("push", "Help shove the wave."), ("back_off", "Walk back toward my tower."))
        else:
            out = self.modes(("farm", "Keep farming: last-hit minions about to die."), ("push", "Shove the wave."),
                             ("back_off", "Walk back toward my tower.")) + self.fight_modes(sc)
        for i, k in enumerate("QWER"):
            if sc.ready.get(k):
                nm = self.ability_names.get(k) or k
                out.append(Spec(k, f"{k} ({nm}) of {self.name}, cast at the chosen spot or unit.", POINT, who="enemy", range=700,
                                run=lambda c, s, t, p, i=i: (c.mi.cast(i + 1, *p), s.name)[1]))
        return out

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if mode == "hold_with_carry":
            mode = "farm"
        return super().continuous(mi, sc, now, aspd, mode, pushing)


# ---------------------------------------------------------------------------------------
class LeeSin(Kit):
    name = "Lee Sin"
    champ_id = 64
    default_role = "JUNGLE"
    skill_order = ["Q", "W", "E", "Q", "Q", "R", "Q", "E", "Q", "E", "R", "E", "E", "W", "W", "R", "W", "W"]
    items = ItemProfile(
        champion="Lee Sin",
        core=["Eclipse", "Black Cleaver", "Sterak's Gage", "Death's Dance", "Guardian Angel", "Maw of Malmortius"],
        starters=["Gustwalker Hatchling", "Health Potion"],
        boots=["Plated Steelcaps", "Mercury's Treads", "Ionian Boots of Lucidity"],
        note="Lee Sin is a jungler who ganks early and plays skirmishes",
    )
    Q_RANGE, E_RADIUS, R_RANGE, W_RANGE = 1100.0, 430.0, 375.0, 700.0

    def __init__(self, role: str = "") -> None:
        super().__init__(role)
        self.q_at = 0.0
        self.e_at = 0.0

    @property
    def jungle(self) -> bool:
        return self.role == "JUNGLE"

    def role_text(self, lane_name: str) -> str:
        return "a Lee Sin jungler" if self.jungle else f"a Lee Sin laner in the {lane_name} lane"

    def q2_up(self, sc: Scene, now: float) -> bool:
        # A Sonic Wave that hit re-lights Q (Resonating Strike) for about 3 s.
        return bool(sc.ready.get("Q")) and 0.3 < now - self.q_at < 3.0

    def _q(self, ctx: Ctx, spec: Spec, tgt, pt) -> str:
        ctx.mi.cast(1, *pt)
        self.q_at = ctx.now
        return "Q sonic wave"

    def specs(self, ctx: Ctx) -> list[Spec]:
        sc, now = ctx.sc, ctx.now
        out = self.modes(
            ("farm", "Keep clearing: attack the camp or wave in front of me."),
            ("back_off", "Walk away from the enemy champion toward safety."),
        ) + self.fight_modes(sc)
        if self.q2_up(sc, now):
            out.append(Spec("Q2_dash", "Q again (Resonating Strike): dash to the unit my Sonic Wave hit and strike it.", NONE,
                            run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.ability(1)), "Q2 dash")[1]))
        elif sc.ready.get("Q") and (sc.minions or sc.champ is not None):
            out.append(Spec("Q", "Q Sonic Wave: skillshot along the chosen line; the first enemy hit is marked so Q2 can dash to it.",
                            POINT, who="enemy", range=self.Q_RANGE, run=self._q))
        if sc.ready.get("E") and (sc.minions or sc.champ is not None):
            out.append(Spec("E", "E Tempest: damage every enemy within about 430 units of me (press again to slow them).", NONE,
                            run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.ability(3)), "E tempest")[1]))
        if sc.ready.get("W"):
            out.append(Spec("W_self", "W Safeguard on myself: a shield now (and lifesteal on the recast).", NONE,
                            run=lambda c, s, t, p: (c.mi.ctl.press(c.mi.kb.self_cast(2)), "W shield self")[1]))
            if ctx.ally_units:
                out.append(Spec("W_ally", "W Safeguard: dash to the chosen allied champion and shield us both.", UNIT,
                                who="ally_champion", range=self.W_RANGE,
                                run=lambda c, s, t, p: (c.mi.cast(2, t.x, t.y), "W to ally")[1]))
        if sc.ready.get("R") and sc.champ is not None:
            out.append(Spec("R", "R Dragon's Rage: kick the enemy champion hard away from me (and into anyone behind them).", UNIT,
                            who="enemy_champion", range=self.R_RANGE,
                            run=lambda c, s, t, p: (c.mi.cast(4, t.unit.x, t.unit.y), "R kick")[1]))
        return out

    def trade_window(self, mi: Micro, sc: Scene, now: float, level_diff: int) -> str | None:
        """A gank: a champion in Sonic Wave reach, Q up, me healthy and not behind in levels."""
        d = sc.champ_dist or 9e9
        if now - getattr(self, "_last_window", 0.0) < 6.0 or level_diff < -1 or mi.hp_pct < 50:
            return None
        if sc.enemy_champs >= 2 and not sc.ally_champs:
            return None  # alone against two: not a gank
        if sc.ready.get("Q") and d <= self.Q_RANGE * 0.9:
            self._last_window = now
            return "all_in"
        return None

    def poke(self, mi: Micro, sc: Scene, now: float, aspd: float) -> bool:
        d = sc.champ_dist or 9e9
        if mi._can_order(now) and sc.ready.get("Q") and not self.q2_up(sc, now) and d <= self.Q_RANGE * 0.9 and now - self.q_at > 3.0:
            x, y = sc.champ.lead(now, 0.25 + d / 1800)
            mi.cast(1, x, y)
            self.q_at = now
            mi._ordered(now, "poke: Q Sonic Wave")
            return True
        return self.poke_auto(mi, sc, now, aspd) or self.continuous(mi, sc, now, aspd, "farm", False)

    def e2_up(self, sc: Scene, now: float) -> bool:
        return bool(sc.ready.get("E")) and 0.35 < now - self.e_at < 3.0

    def execute(self, mi: Micro, sc: Scene, tr, now: float) -> bool:
        d = sc.dist(tr)
        if sc.ready.get("R") and d <= self.R_RANGE + 40:
            mi.cast(4, tr.unit.x, tr.unit.y)
            mi._ordered(now, "execute: R kick")
            return True
        if self.q2_up(sc, now) and d <= 1300 and now - self.q_at > 0.35:
            mi.ctl.press(mi.kb.ability(1))
            self.q_at = 0.0
            mi._ordered(now, "execute: Q2 onto the low champion")
            return True
        if sc.ready.get("E") and not self.e2_up(sc, now) and d <= self.E_RADIUS - 40:
            mi.ctl.press(mi.kb.ability(3))
            self.e_at = now
            mi._ordered(now, "execute: E Tempest")
            return True
        if sc.ready.get("Q") and not self.q2_up(sc, now) and d <= self.Q_RANGE * 0.9:
            x, y = tr.lead(now, 0.25 + d / 1800)
            mi.cast(1, x, y)
            self.q_at = now
            mi._ordered(now, "execute: Q Sonic Wave")
            return True
        return super().execute(mi, sc, tr, now)

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        """Lee Sin's gank / skirmish combo, one order per tick: R to finish (or to peel when I am
        losing); Q2 dash after a Sonic Wave hit; Q1 from range (led); E then E2 slow in melee;
        W shield when hurt; autos between spells (the passive gives two fast ones); chase."""
        if mode == "poke":
            return self.poke(mi, sc, now, aspd)
        if mode == "trade" and self.trade_over(mi, sc, now):
            return True
        if not mi._can_order(now):
            return True
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        if rdy.get("R") and d <= self.R_RANGE + 60 and (ch.unit.hp < 0.3 or (mode == "all_in" and mi.hp_pct < 35)):
            mi.cast(4, ch.unit.x, ch.unit.y)
            mi._ordered(now, f"{mode}: R kick")
            self.burst_at = now
            return True
        if self.q2_up(sc, now) and d <= 1300 and now - self.q_at > 0.35:
            mi.ctl.press(mi.kb.ability(1))
            self.q_at = 0.0
            mi._ordered(now, f"{mode}: Q2 dash to the champion")
            self.burst_at = self.spell_at = now
            self.autos_since = 0
            return True
        # Flurry: two quick autos after each spell before the next one (energy lasts, damage goes up).
        if (d <= 250 and now - getattr(self, "spell_at", 0.0) < 3.0 and getattr(self, "autos_since", 2) < 2
                and mi.attack_ready(now, aspd)):
            mi.attack(ch, now, f"{mode}: flurry auto")
            self.autos_since = getattr(self, "autos_since", 0) + 1
            return True
        if rdy.get("Q") and not self.q2_up(sc, now) and d <= self.Q_RANGE * 0.9 and now - self.q_at > 3.0:
            x, y = ch.lead(now, 0.25 + d / 1800)
            mi.cast(1, x, y)
            self.q_at = self.spell_at = now
            self.autos_since = 0
            mi._ordered(now, f"{mode}: Q Sonic Wave at the champion")
            return True
        if self.e2_up(sc, now) and d <= 500 and mode == "all_in":
            mi.ctl.press(mi.kb.ability(3))
            self.e_at = 0.0
            self.spell_at, self.autos_since = now, 0
            mi._ordered(now, f"{mode}: E2 cripple")
            return True
        if rdy.get("E") and not self.e2_up(sc, now) and d <= self.E_RADIUS - 40 and now - self.e_at > 3.0:
            mi.ctl.press(mi.kb.ability(3))
            self.e_at = now
            self.burst_at = self.spell_at = now
            self.autos_since = 0
            mi._ordered(now, f"{mode}: E Tempest")
            return True
        if rdy.get("W") and mi.hp_pct < 45 and d <= 600:
            mi.ctl.press(mi.kb.self_cast(2))
            mi._ordered(now, f"{mode}: W shield")
            return True
        if self.ignite_if_kill(mi, sc, now, mode):
            return True
        return self.hit_or_chase(mi, sc, now, aspd, mode, reach=190.0)

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        return {"Q": "Q2 dash available" if self.q2_up(sc, now) else self.ready_words(sc, "Q"),
                "W": self.ready_words(sc, "W"), "E": self.ready_words(sc, "E"), "R": self.ready_words(sc, "R")}

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if self.jungle and mode == "farm" and sc.minions:
            # Clearing a camp: E when monsters are in reach, Q the healthiest one, otherwise attack.
            near = [t for t in sc.minions if sc.dist(t) <= self.E_RADIUS]
            if sc.ready.get("W") and near and mi.hp_pct < 60 and now - getattr(self, "_w_at", 0.0) > 4 and mi._can_order(now):
                mi.ctl.press(mi.kb.self_cast(2))  # shield, and the recast's lifesteal, while the camp hits back
                self._w_at = now
                mi._ordered(now, "W shield on camp")
                return True
            if sc.ready.get("E") and near and now - self.e_at > 0.6 and mi._can_order(now):
                mi.ctl.press(mi.kb.ability(3))
                self.e_at = now
                mi._ordered(now, "E on camp")
                return True
            if sc.ready.get("Q") and now - self.q_at > 3.5 and mi._can_order(now):
                big = max(sc.minions, key=lambda t: t.unit.hp)
                mi.cast(1, big.unit.x, big.unit.y)
                self.q_at = now
                mi._ordered(now, "Q on camp")
                return True
            if self.q2_up(sc, now) and mi._can_order(now):
                mi.ctl.press(mi.kb.ability(1))
                mi._ordered(now, "Q2 on camp")
                return True
            if mi.attack_ready(now, aspd):
                tgt = min(sc.minions, key=lambda t: (t.unit.hp, sc.dist(t)))
                mi.attack(tgt, now, f"attack camp ({tgt.unit.kind} {int(tgt.unit.hp * 100)}% at {int(sc.dist(tgt))}u)", right_click=True)
            return True
        return super().continuous(mi, sc, now, aspd, mode, pushing)


KITS = {"yasuo": Yasuo, "thresh": Thresh, "soraka": Soraka, "leesin": LeeSin}
BY_ID = {Yasuo.champ_id: Yasuo, Thresh.champ_id: Thresh, Soraka.champ_id: Soraka, LeeSin.champ_id: LeeSin}


def kit_for(champion_name: str, role: str = "", ability_names: dict | None = None) -> Kit:
    cls = KITS.get((champion_name or "").lower().replace(" ", "").replace("'", ""))
    if cls is None:
        return Generic(champion_name, role, ability_names)
    return cls(role)
