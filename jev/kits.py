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

from typesafe_sdk import Choice

from jev import config
from jev.items import ItemProfile
from jev.micro import Micro, Scene

VC = config.VISION
FAST = config.FAST

YASUO_ID, THRESH_ID = 157, 412


class Kit:
    name = "?"
    champ_id = 0
    default_role = "MIDDLE"
    skill_order: list[str] = []
    items: ItemProfile
    actions: dict[str, str] = {}

    def __init__(self, role: str = "") -> None:
        self.role = (role or self.default_role).upper()

    @property
    def support(self) -> bool:
        return self.role == "UTILITY"

    def role_text(self, lane_name: str) -> str:
        what = "support" if self.support else "laner"
        return f"a {self.name} {what} in the {lane_name} lane"

    # -- tactical head -------------------------------------------------------------------
    def available(self, sc: Scene, mi: Micro, now: float) -> list[str]:
        raise NotImplementedError

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        return {}

    def heads(self, sc: Scene, acts: list[str]) -> dict[str, Choice]:
        return {}

    def read_heads(self, res: Any, heads: dict[str, Choice]) -> dict[str, Any]:
        return {}

    def execute(self, mi: Micro, option: str, sc: Scene, now: float, aspd: float, extra: dict) -> bool:
        raise NotImplementedError

    def reflex(self, mi: Micro, sc: Scene, now: float, plan: dict) -> bool:
        return False

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        """The standing behaviour between one-shot actions."""
        if mode == "back_off" and sc.champ is not None:
            mi.back_off(sc, now)
            return True
        if self.support and mode != "push":
            return mi.support_step(sc, now, aspd)
        return mi.farm_step(sc, now, aspd, push=(mode == "push" or pushing))

    def ready_words(self, sc: Scene, key: str) -> str:
        return "ready" if sc.ready.get(key) else "not ready"


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
    actions = {
        "farm": "Keep farming: last-hit minions that are about to die, otherwise hold just behind the wave.",
        "push": "Shove the wave: attack minions freely, ignore last-hit timing.",
        "q_minions": "Q into the minions: last-hits what Q can kill and builds Q stacks toward the tornado.",
        "poke_q": "Q the enemy champion: a quick poke that also builds a Q stack.",
        "tornado": "Throw the Q3 tornado at the enemy champion: knocks them up and enables R.",
        "eq_champion": "E through the enemy champion and Q during the dash (circle Q): an all-in opener.",
        "gapclose": "E through a minion toward the enemy champion (Q during the dash if ready) to get in range.",
        "auto_champion": "Auto-attack the enemy champion.",
        "ult": "R (Last Breath) on the airborne enemy champion: big damage, only possible while they are knocked up.",
        "wind_wall": "W wind wall toward the enemy champion to block their projectiles and skillshots.",
        "back_off": "Walk back toward my tower, away from the enemy champion.",
    }

    def __init__(self, role: str = "") -> None:
        super().__init__(role)
        self.q = QStacks()
        self.tornado_at = 0.0

    def available(self, sc: Scene, mi: Micro, now: float) -> list[str]:
        q3 = self.q.q3(now)
        acts = ["farm", "back_off"]
        if sc.minions:
            acts.append("push")
            if sc.ready.get("Q"):
                acts.append("q_minions")
        c, d = sc.champ, sc.champ_dist
        if c is not None and d is not None:
            if sc.ready.get("Q") and not q3 and d <= VC.q_range:
                acts.append("poke_q")
            if sc.ready.get("Q") and q3 and d <= VC.q3_range:
                acts.append("tornado")
            if sc.ready.get("E") and d <= VC.e_range and c.e_marked_until < now:
                acts.append("eq_champion")
            if sc.ready.get("E") and sc.dash_toward is not None:
                acts.append("gapclose")
            if d <= VC.auto_range + 150:
                acts.append("auto_champion")
            if sc.r_lit and d <= VC.r_range:
                acts.append("ult")
            if sc.ready.get("W") and d <= 1100:
                acts.append("wind_wall")
        return acts

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        q3 = self.q.q3(now)
        return {
            "Q": ("Q3 tornado ready" if q3 else "ready") if sc.ready.get("Q") else "on cooldown",
            "W": self.ready_words(sc, "W"), "E": self.ready_words(sc, "E"),
            "R": "castable (enemy airborne)" if sc.r_lit else "not castable",
            "flash": self.ready_words(sc, "D"),
        }

    def heads(self, sc: Scene, acts: list[str]) -> dict[str, Choice]:
        if "gapclose" in acts and len(sc.dash_options) >= 2:
            return {"dash_target": Choice(
                instructions="If Yasuo dashes through a minion to reach the enemy champion, which minion?",
                criteria={f"minion_{tr.id}": (f"{int(tr.unit.hp * 100)}% HP, {int(sc.dist(tr))} units from me, "
                                              f"lands {int(g)} units closer to the enemy champion")
                          for tr, g in sc.dash_options},
            )}
        return {}

    def read_heads(self, res: Any, heads: dict[str, Choice]) -> dict[str, Any]:
        if "dash_target" not in heads:
            return {}
        try:
            return {"dash_target": int(str(res.choices["dash_target"].choice).split("_")[1])}
        except (KeyError, IndexError, ValueError):
            return {}

    def _q(self, mi: Micro, sc: Scene, x: float, y: float, now: float, what: str) -> None:
        was_q3 = self.q.q3(now)
        hit = _line_hits(sc, x, y, VC.q3_range if was_q3 else VC.q_range)
        mi.cast(1, x, y)
        self.q.cast(hit, now)
        if was_q3:
            self.tornado_at = now
        mi._ordered(now, what)

    def _e(self, mi: Micro, tr, now: float, what: str, then_q: bool) -> None:
        mi.cast(3, tr.unit.x, tr.unit.y)
        tr.e_marked_until = now + 10.0
        if then_q:
            time.sleep(FAST.eq_delay_s)
            mi.ctl.press(mi.kb.ability(1))  # Q during the dash: circular Q around Yasuo
            self.q.cast(True, now)
            what += "+Q"
        mi._ordered(now, what)

    def execute(self, mi: Micro, option: str, sc: Scene, now: float, aspd: float, extra: dict) -> bool:
        champ = sc.champ
        dash = extra.get("dash_target")
        if option == "gapclose" and dash is not None:
            chosen = next((o[0] for o in sc.dash_options if o[0].id == dash), None)
            if chosen is not None:
                sc.dash_toward = chosen
        if option == "ult" and champ is not None and sc.r_lit:
            mi.cast(4, champ.unit.x, champ.unit.y)
            mi._ordered(now, "R: Last Breath")
        elif option == "tornado" and champ is not None and sc.ready.get("Q"):
            self._q(mi, sc, champ.unit.x, champ.unit.y, now, "Q3 tornado at champion")
        elif option == "poke_q" and champ is not None and sc.ready.get("Q"):
            self._q(mi, sc, champ.unit.x, champ.unit.y, now, "Q champion")
        elif option == "q_minions" and sc.minions and sc.ready.get("Q"):
            tgt = sc.killable_q[0] if sc.killable_q else min(sc.minions, key=sc.dist)
            self._q(mi, sc, tgt.unit.x, tgt.unit.y, now, "Q minions")
        elif option == "eq_champion" and champ is not None and sc.ready.get("E"):
            self._e(mi, champ, now, "E champion", then_q=bool(sc.ready.get("Q")))
        elif option == "gapclose" and sc.dash_toward is not None and sc.ready.get("E"):
            self._e(mi, sc.dash_toward, now, "E minion toward champion", then_q=bool(sc.ready.get("Q")))
        elif option == "wind_wall" and champ is not None and sc.ready.get("W"):
            mi.cast(2, champ.unit.x, champ.unit.y)
            mi._ordered(now, "W wind wall toward champion")
        elif option == "auto_champion" and champ is not None and mi.attack_ready(now, aspd):
            mi.attack(champ, now, "auto champion")
        else:
            return False
        return True

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
    HOOK_RANGE, FLAY_RANGE, BOX_RADIUS, LANTERN_RANGE, AUTO_RANGE = 1050.0, 450.0, 420.0, 950.0, 480.0
    actions = {
        "hold_with_carry": "Stay beside my carry, zone the enemy, and do not take last hits.",
        "push": "Help shove the wave: attack minions.",
        "hook": "Q Death Sentence at the enemy champion: a long skillshot that stuns and pulls them if it lands.",
        "follow_hook": "Q again to fly to the hooked enemy champion and engage (only right if my carry can follow).",
        "flay_pull": "E Flay backward: sweep the enemy champion toward me and my carry (engage).",
        "flay_push": "E Flay toward the enemy champion: knock them away from me (peel, disengage).",
        "lantern_to_carry": "W Dark Passage on my carry: shield them, or let them click the lantern to reach me.",
        "box": "R The Box: walls around me that slow and damage enemies who cross them.",
        "auto_champion": "Auto-attack the enemy champion (poke).",
        "back_off": "Walk back toward my tower, away from the enemy champion.",
    }

    def __init__(self, role: str = "") -> None:
        super().__init__(role)
        self.hook_at = 0.0

    def hooked(self, sc: Scene, now: float) -> bool:
        # A landed hook re-lights Q (the recast) for about 1.5 s.
        return bool(sc.ready.get("Q")) and 0.25 < now - self.hook_at < 1.8

    def available(self, sc: Scene, mi: Micro, now: float) -> list[str]:
        acts = ["hold_with_carry", "back_off"]
        if sc.minions:
            acts.append("push")
        c, d = sc.champ, sc.champ_dist
        if self.hooked(sc, now):
            acts.append("follow_hook")
        if c is not None and d is not None:
            if sc.ready.get("Q") and not self.hooked(sc, now) and d <= self.HOOK_RANGE:
                acts.append("hook")
            if sc.ready.get("E") and d <= self.FLAY_RANGE:
                acts += ["flay_pull", "flay_push"]
            if sc.ready.get("R") and d <= self.BOX_RADIUS:
                acts.append("box")
            if d <= self.AUTO_RANGE:
                acts.append("auto_champion")
        if sc.ready.get("W") and sc.ally_champs:
            acts.append("lantern_to_carry")
        return acts

    def me_state(self, sc: Scene, mi: Micro, now: float) -> dict:
        return {
            "Q_hook": ("hook landed, can fly to target" if self.hooked(sc, now) else self.ready_words(sc, "Q")),
            "W_lantern": self.ready_words(sc, "W"), "E_flay": self.ready_words(sc, "E"),
            "R_box": self.ready_words(sc, "R"), "flash": self.ready_words(sc, "D"),
            "carry_on_screen": bool(sc.ally_champs),
            "carry_hp_percent": int(min(sc.ally_champs, key=lambda u: math.hypot(u.x - sc.me_xy[0], u.y - sc.me_xy[1])).hp * 100) if sc.ally_champs else None,
        }

    def execute(self, mi: Micro, option: str, sc: Scene, now: float, aspd: float, extra: dict) -> bool:
        champ = sc.champ
        mx, my = sc.me_xy
        if option == "hook" and champ is not None and sc.ready.get("Q"):
            mi.cast(1, champ.unit.x, champ.unit.y)
            self.hook_at = now
            mi._ordered(now, "Q hook at champion")
        elif option == "follow_hook" and self.hooked(sc, now):
            mi.ctl.press(mi.kb.ability(1))
            mi._ordered(now, "Q2 fly to hooked target")
        elif option in ("flay_pull", "flay_push") and champ is not None and sc.ready.get("E"):
            dx, dy = champ.unit.x - mx, champ.unit.y - my
            n = math.hypot(dx, dy) or 1.0
            if option == "flay_pull":
                x, y = mx - dx / n * 150, my - dy / n * 150   # cast behind: the sweep pulls them in
            else:
                x, y = champ.unit.x, champ.unit.y            # cast at them: knocks them away
            mi.cast(3, x, y)
            mi._ordered(now, option.replace("_", " "))
        elif option == "lantern_to_carry" and sc.ally_champs and sc.ready.get("W"):
            carry = min(sc.ally_champs, key=lambda u: math.hypot(u.x - mx, u.y - my))
            mi.cast(2, carry.x, carry.y)
            mi._ordered(now, "W lantern to carry")
        elif option == "box" and champ is not None and sc.ready.get("R"):
            mi.cast(4, mx, my)
            mi._ordered(now, "R box")
        elif option == "auto_champion" and champ is not None and mi.attack_ready(now, aspd):
            mi.attack(champ, now, "auto champion")
        else:
            return False
        return True

    def continuous(self, mi: Micro, sc: Scene, now: float, aspd: float, mode: str, pushing: bool) -> bool:
        if mode == "hold_with_carry":
            mode = "farm"
        return super().continuous(mi, sc, now, aspd, mode, pushing)


KITS = {"yasuo": Yasuo, "thresh": Thresh}
BY_ID = {Yasuo.champ_id: Yasuo, Thresh.champ_id: Thresh}


def kit_for(champion_name: str, role: str = "") -> Kit:
    cls = KITS.get((champion_name or "").lower().replace(" ", ""), Yasuo)
    return cls(role)
