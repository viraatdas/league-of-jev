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
FIGHT_MODES = ("trade", "all_in")


class Kit:
    """Base kit. specs(ctx) lists the champion's own moves (abilities, combos, standing modes);
    the universal moves, summoner spells and items are added by the tactical head."""

    name = "?"
    champ_id = 0
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
        if mode in FIGHT_MODES:
            if sc.champ is not None:
                self._fight_seen = now
                return self.fight(mi, sc, now, aspd, mode)
            if now - getattr(self, "_fight_seen", 0.0) > 1.5:
                mi.set_mode("farm", now)
                mode = "farm"
        return self.continuous(mi, sc, now, aspd, mode, pushing)

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        if mode == "trade" and self.trade_over(mi, sc, now):
            return True
        return self.hit_or_chase(mi, sc, now, aspd, mode)

    def trade_over(self, mi: Micro, sc: Scene, now: float) -> bool:
        """A trade ends 0.7 s after the burst (one more auto), or 2.5 s after it began; then walk
        back for 1.2 s and return to farming. True while the trade is winding down."""
        t0 = mi.mode_since
        burst = getattr(self, "burst_at", 0.0)
        end = (burst + 0.7) if burst >= t0 else (t0 + 2.5)
        if now < end:
            return False
        if now < end + 1.2:
            mi.back_off(sc, now)
            mi.last_action = "trade: step back"
            return True
        mi.set_mode("farm", now)
        return False

    def hit_or_chase(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, reach: float = 0.0) -> bool:
        """Auto the champion when in range, otherwise walk onto them (orb-walk: move between autos)."""
        ch, d = sc.champ, sc.champ_dist or 9e9
        rng = (reach or VC.auto_range) + 60
        if d <= rng and mi.attack_ready(now, aspd):
            mi.attack(ch, now, f"{mode}: auto the champion")
            return True
        if mi.in_windup(now, aspd):
            return True
        x, y = ch.lead(now, 0.25)
        mi.move_screen(x, y, now, f"{mode}: stick to the champion" if d <= rng else f"{mode}: chase", every=0.12)
        return True

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

    def q3(self, now: float) -> bool:
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

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        # Farming Yasuo uses Q on cooldown on minions it can kill (standard play: CS and Q stacks).
        if mode in ("farm", "push") and sc.ready.get("Q") and sc.killable_q and mi._can_order(now):
            tq = sc.killable_q[0]
            was_q3 = self.q.q3(now)
            hit = _line_hits(sc, tq.unit.x, tq.unit.y, VC.q3_range if was_q3 else VC.q_range)
            mi.cast(1, tq.unit.x, tq.unit.y)
            self.q.cast(hit, now)
            mi._ordered(now, "Q last hit")
            mi.lh_pending.append((now, "Q", tq.unit.hp))
            mi.last_exec = {"name": "Q last hit", "what": "Q last hit", "ts": now, "target": mi._pt(tq.unit.x, tq.unit.y), "point": None}
            return True
        return super().continuous(mi, sc, now, aspd, mode, pushing)

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        """Yasuo's combo, one order per tick: R on an airborne target; Q3 tornado from range (led
        onto where they walk); E onto them with Q in the dash (EQ, a knock-up with Q3); Q in
        melee range; ignite a kill; E through a minion toward them to close the gap; autos."""
        if mode == "trade" and self.trade_over(mi, sc, now):
            return True
        if not mi._can_order(now):
            return True
        ch, d, rdy = sc.champ, sc.champ_dist or 9e9, sc.ready
        q3 = self.q.q3(now)
        if sc.r_lit and d <= VC.r_range:
            mi.cast(4, ch.unit.x, ch.unit.y)
            mi._ordered(now, f"{mode}: R Last Breath")
            self.burst_at = now
            return True
        if rdy.get("Q") and q3 and d <= VC.q3_range * 0.92 and d > VC.e_range:
            x, y = ch.lead(now, 0.3 + d / 1500)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            self.tornado_at = now
            mi._ordered(now, f"{mode}: Q3 tornado")
            return True
        if rdy.get("E") and d <= VC.e_range and ch.e_marked_until <= now:
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
            x, y = ch.lead(now, 0.25)
            mi.cast(1, x, y)
            self.q.cast(True, now)
            if q3:
                self.tornado_at = now
            self.burst_at = now
            mi._ordered(now, f"{mode}: Q the champion")
            return True
        if self.ignite_if_kill(mi, sc, now, mode):
            return True
        if rdy.get("E") and d > VC.auto_range + 120 and sc.dash_options and (mode == "all_in" or rdy.get("Q")):
            tr = sc.dash_options[0][0]
            mi.cast(3, tr.unit.x, tr.unit.y)
            tr.e_marked_until = now + 10.0
            mi._ordered(now, f"{mode}: E through a minion toward the champion")
            return True
        return self.hit_or_chase(mi, sc, now, aspd, mode)

    def reflex(self, mi: Micro, sc: Scene, now: float, plan: dict) -> bool:
        """R the moment our own tornado lifts the target, if the fight read is favourable."""
        if sc.r_lit and sc.champ is not None and now - self.tornado_at < FAST.r_watch_s and plan.get("fight_favorable", 0.5) >= 0.45:
            mi.cast(4, sc.champ.unit.x, sc.champ.unit.y)
            mi._ordered(now, "R reflex after tornado")
            self.tornado_at = 0.0
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

    def e2_up(self, sc: Scene, now: float) -> bool:
        return bool(sc.ready.get("E")) and 0.35 < now - self.e_at < 3.0

    def fight(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str) -> bool:
        """Lee Sin's gank / skirmish combo, one order per tick: R to finish (or to peel when I am
        losing); Q2 dash after a Sonic Wave hit; Q1 from range (led); E then E2 slow in melee;
        W shield when hurt; autos between spells (the passive gives two fast ones); chase."""
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
            self.burst_at = now
            return True
        if rdy.get("Q") and not self.q2_up(sc, now) and d <= self.Q_RANGE * 0.9 and now - self.q_at > 3.0:
            x, y = ch.lead(now, 0.25 + d / 1800)
            mi.cast(1, x, y)
            self.q_at = now
            mi._ordered(now, f"{mode}: Q Sonic Wave at the champion")
            return True
        if self.e2_up(sc, now) and d <= 500 and mode == "all_in":
            mi.ctl.press(mi.kb.ability(3))
            self.e_at = 0.0
            mi._ordered(now, f"{mode}: E2 cripple")
            return True
        if rdy.get("E") and not self.e2_up(sc, now) and d <= self.E_RADIUS - 40 and now - self.e_at > 3.0:
            mi.ctl.press(mi.kb.ability(3))
            self.e_at = now
            self.burst_at = now
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
                mi.attack(tgt, now, f"attack camp ({tgt.unit.kind} {int(tgt.unit.hp * 100)}% at {int(sc.dist(tgt))}u)")
            return True
        return super().continuous(mi, sc, now, aspd, mode, pushing)


KITS = {"yasuo": Yasuo, "thresh": Thresh, "soraka": Soraka, "leesin": LeeSin}
BY_ID = {Yasuo.champ_id: Yasuo, Thresh.champ_id: Thresh, Soraka.champ_id: Soraka, LeeSin.champ_id: LeeSin}


def kit_for(champion_name: str, role: str = "", ability_names: dict | None = None) -> Kit:
    cls = KITS.get((champion_name or "").lower().replace(" ", "").replace("'", ""))
    if cls is None:
        return Generic(champion_name, role, ability_names)
    return cls(role)
