"""The full tactical action space, factorised like OpenAI Five's (arXiv 1912.06680, Appendix F).

Each tactical step Jev answers, in one parallel call:
  action    which move: every ability, the kit's combos, both summoner spells, every usable
            item active, potion, ward, move, attack, attack-move, stop, hold, and the standing
            modes (farm, push, stay with the carry, back off). Filtered to what is possible now.
  target    which visible unit: enemy champions, enemy minions, allied champions.
  where     where on the ground: at the target, on myself, up or down the lane, or one of eight
            compass directions (north is up on screen).
  distance  how far for a ground point: short, medium, or the move's full range.
Five picked the target and offset heads conditioned on the action. Jev's questions are answered
in parallel, so the executor masks each head's probabilities by what the chosen action accepts
and takes the most likely valid answer (an enemy-only spell never lands on an ally even if the
target head's top pick was the carry).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from typesafe_sdk import Choice, Score

from jev import config
from jev.micro import Micro, Scene, Track
from jev.vision import Unit

VC = config.VISION

NONE, POINT, UNIT = "none", "point", "unit"
COMPASS = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
WHERE: dict[str, str] = {
    "at_target": "At the chosen target unit.",
    "on_myself": "Where I stand.",
    "toward_enemy_tower": "Up the lane, toward the enemy tower.",
    "toward_my_tower": "Back down the lane, toward my tower.",
    **{d: f"{d} of me (north is up on screen)." for d in ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]},
}
DISTANCES = [150.0, 350.0, None]  # units; None = the move's full range


@dataclass
class Spec:
    """One primary action. `target` says which heads it reads: none, a ground point, or a unit.
    `who` limits unit targets (and 'at_target' points): enemy, enemy_champion, enemy_minion,
    ally_champion. `run` executes it; `mode` marks a standing behaviour instead of a one-shot."""

    name: str
    text: str
    target: str = NONE
    who: str = "enemy"
    range: float = 600.0
    run: Callable[["Ctx", "Spec", Any, tuple[float, float] | None], str] | None = None
    mode: str | None = None
    accept: Callable[[str, Any], bool] | None = None   # extra per-unit filter (e.g. not dashed recently)


@dataclass
class Ctx:
    mi: Micro
    sc: Scene
    kit: Any
    lane: Any
    now: float
    aspd: float
    ally_units: list[Unit] = field(default_factory=list)
    lane_progress: float = 0.5
    summoners: list[str | None] = field(default_factory=list)
    plan: dict = field(default_factory=dict)

    def flash_slot(self) -> int | None:
        """1 or 2 when Flash is equipped and ready, else None."""
        for i, (name, hud) in enumerate(zip(self.summoners, "DF")):
            if name == "flash" and self.sc.ready.get(hud):
                return i + 1
        return None

    def flash_toward(self, x: float, y: float) -> None:
        slot = self.flash_slot()
        if slot is not None:
            self.mi.ctl.cast(self.mi.kb.summoner(slot), *self.mi._pt(x, y), self.mi.kb.quick(f"evtCastAvatarSpell{slot}"))


# -- geometry ----------------------------------------------------------------------------
def compass(dx: float, dy: float) -> str:
    """Screen vector (y down) to one of eight compass points, north = up."""
    ang = math.degrees(math.atan2(-dy, dx)) % 360
    return COMPASS[int(((ang + 22.5) % 360) // 45)]


def _dir(name: str) -> tuple[float, float]:
    i = COMPASS.index(name)
    a = math.radians(i * 45)
    return math.cos(a), -math.sin(a)


def unit_xy(u) -> tuple[float, float]:
    uu = u.unit if isinstance(u, Track) else u
    return uu.x, uu.y


def dist_units(sc: Scene, u) -> float:
    x, y = unit_xy(u)
    return math.hypot(x - sc.me_xy[0], y - sc.me_xy[1]) / VC.px_per_unit


# -- the target list ---------------------------------------------------------------------
def candidates(ctx: Ctx, max_minions: int = 6) -> dict[str, tuple[str, Any]]:
    """label -> (kind, unit). Enemy champions, the most relevant enemy minions (killable first,
    then nearest), allied champions."""
    sc = ctx.sc
    out: dict[str, tuple[str, Any]] = {}
    for tr in sorted([sc.champ] if sc.champ else [], key=lambda t: dist_units(sc, t)):
        out[f"enemy_champion_{tr.id}"] = ("enemy_champion", tr)
    killable = {t.id for t in sc.killable_auto}
    ms = sorted(sc.minions, key=lambda t: (t.id not in killable, dist_units(sc, t)))[:max_minions]
    for tr in ms:
        out[f"minion_{tr.id}"] = ("enemy_minion", tr)
    for i, u in enumerate(sorted(ctx.ally_units, key=lambda u: dist_units(sc, u))):
        out[f"ally_champion_{i + 1}"] = ("ally_champion", u)
    return out


def describe(ctx: Ctx, label: str, kind: str, u, opp_name: str) -> str:
    sc = ctx.sc
    x, y = unit_xy(u)
    hp = (u.unit.hp if isinstance(u, Track) else u.hp) * 100
    where = f"{int(round(dist_units(sc, u) / 25) * 25)} units {compass(x - sc.me_xy[0], y - sc.me_xy[1])}"
    if kind == "enemy_champion":
        return f"{opp_name} (enemy champion), {hp:.0f}% HP, {where}"
    if kind == "enemy_minion":
        extra = ", killable by one auto" if u in sc.killable_auto else ""
        dashed = ", dashed through recently" if isinstance(u, Track) and u.e_marked_until > ctx.now else ""
        return f"enemy minion, {hp:.0f}% HP, {where}{extra}{dashed}"
    return f"allied champion, {hp:.0f}% HP, {where}"


def valid(spec: Spec, kind: str) -> bool:
    w = spec.who
    return (w == "any" or w == kind or (w == "enemy" and kind.startswith("enemy")))


# -- universal moves ---------------------------------------------------------------------
def _move(ctx: Ctx, spec: Spec, tgt, pt) -> str:
    ctx.mi.ctl.move_to(*ctx.mi._pt(*pt))
    ctx.mi.last_move = ctx.now
    return "move"


def _attack(ctx: Ctx, spec: Spec, tgt, pt) -> str:
    if not ctx.mi.attack_ready(ctx.now, ctx.aspd):
        return ""
    ctx.mi.attack(tgt, ctx.now, "attack")
    return "attack"


def _attack_move(ctx: Ctx, spec: Spec, tgt, pt) -> str:
    x, y = ctx.mi._pt(*pt)
    ctx.mi.ctl.attack_move(ctx.mi.kb.attack_move, x, y)
    return "attack-move"


def _key(bind_fn: Callable[[Any], Any]) -> Callable:
    def run(ctx: Ctx, spec: Spec, tgt, pt) -> str:
        ctx.mi.ctl.press(bind_fn(ctx.mi.kb))
        return spec.name
    return run


def cast_run(bind_fn: Callable[[Any], Any], quick_event: str) -> Callable:
    """Cast a key at the target unit or ground point (quick cast: cursor then key; otherwise the
    key and a confirming click). With no target, the key alone (self-cast / instant)."""
    def run(ctx: Ctx, spec: Spec, tgt, pt) -> str:
        kb = ctx.mi.kb
        bind = bind_fn(kb)
        if spec.target == NONE or (tgt is None and pt is None):
            ctx.mi.ctl.press(bind)
        else:
            x, y = unit_xy(tgt) if (spec.target == UNIT and tgt is not None) else pt
            ctx.mi.ctl.cast(bind, *ctx.mi._pt(x, y), kb.quick(quick_event))
        return spec.name
    return run


def universal(ctx: Ctx) -> list[Spec]:
    sc = ctx.sc
    out = [
        Spec("move", "Walk to the chosen point.", POINT, range=600, run=_move),
        Spec("attack_move", "Walk toward the chosen point, attacking the first enemy on the way.", POINT, range=500, run=_attack_move),
    ]
    if sc.champ is not None:
        # Standing still is only a move with an enemy champion in view (bait, wait out a skillshot);
        # offered always, Jev picked it in base and the champion idled into an AFK warning.
        out += [Spec("stop", "Stop moving and attacking right now (cancel).", NONE, run=_key(lambda kb: kb.stop)),
                Spec("hold", "Hold position: stay still, only attack enemies already in range.", NONE, run=_key(lambda kb: kb.hold))]
    if sc.minions or sc.champ is not None:
        out.append(Spec("attack", "Basic attack the chosen enemy unit.", UNIT, who="enemy", range=VC.auto_range + 300, run=_attack))
    return out


# -- summoner spells ---------------------------------------------------------------------
# name -> (target type, who, range, what it does). Internal names from rawDisplayName too.
SUMMONERS: dict[str, tuple[str, str, float, str]] = {
    "flash": (POINT, "enemy", 400, "Blink about 400 units in the chosen direction (escape, or close a gap for a kill)."),
    "ignite": (UNIT, "enemy_champion", 600, "Burn an enemy champion: true damage over 5 s and less healing (finish a kill)."),
    "exhaust": (UNIT, "enemy_champion", 650, "Slow an enemy champion and cut their damage (survive a duel)."),
    "heal": (NONE, "any", 0, "Heal myself (and a nearby ally) and gain move speed."),
    "barrier": (NONE, "any", 0, "Shield myself for a moment against burst."),
    "ghost": (NONE, "any", 0, "Gain large move speed for a few seconds (chase or escape)."),
    "cleanse": (NONE, "any", 0, "Remove stuns and slows from myself."),
    "smite": (UNIT, "enemy_minion", 500, "Deal big true damage to a minion or monster."),
}
RAW = {"SummonerFlash": "flash", "SummonerDot": "ignite", "SummonerExhaust": "exhaust", "SummonerHeal": "heal",
       "SummonerBarrier": "barrier", "SummonerHaste": "ghost", "SummonerBoost": "cleanse", "SummonerSmite": "smite"}


def summoner_names(me_player: dict) -> list[str | None]:
    out: list[str | None] = []
    ss = me_player.get("summonerSpells") or {}
    for k in ("summonerSpellOne", "summonerSpellTwo"):
        s = ss.get(k) or {}
        name = str(s.get("displayName", "")).lower()
        raw = str(s.get("rawDisplayName", ""))
        for key, val in RAW.items():
            if key in raw:
                name = val
        out.append(name if name in SUMMONERS else None)
    return out


def summoner_specs(ctx: Ctx, names: list[str | None]) -> list[Spec]:
    out = []
    for i, (name, hud) in enumerate(zip(names, "DF")):
        if name is None or not ctx.sc.ready.get(hud):
            continue
        tt, who, rng, text = SUMMONERS[name]
        if tt == UNIT and who == "enemy_champion" and ctx.sc.champ is None:
            continue
        if name == "ignite" and ctx.sc.champ.unit.hp > 0.45:
            continue  # ignite is a finisher: burned at full HP it only wasted the spell (bot game 4: 3 uses, no kills)
        out.append(Spec(name, f"{hud}: {text}", tt, who=who, range=rng,
                        run=cast_run(lambda kb, i=i: kb.summoner(i + 1), f"evtCastAvatarSpell{i + 1}")))
    return out


# -- items ---------------------------------------------------------------------------------
# Actives that need a target; everything else with an active is self-cast.
ITEM_TARGETS: dict[str, tuple[str, str, float]] = {
    "hextech rocketbelt": (POINT, "enemy", 275), "redemption": (POINT, "any", 5500), "stridebreaker": (POINT, "enemy", 450),
    "galeforce": (POINT, "enemy", 425), "everfrost": (POINT, "enemy", 900), "control ward": (POINT, "any", 600),
    "stealth ward": (POINT, "any", 600), "farsight alteration": (POINT, "any", 4000),
    "knight's vow": (UNIT, "ally_champion", 1000), "mikael's blessing": (UNIT, "ally_champion", 650),
    "hextech gunblade": (UNIT, "enemy_champion", 700), "bilgewater cutlass": (UNIT, "enemy_champion", 550),
}
POTIONS = ("health potion", "refillable potion", "corrupting potion", "total biscuit")


def item_specs(ctx: Ctx, items: list[dict], hud_ready: dict[int, bool], hp_pct: float, potion_used_at: float) -> list[Spec]:
    """Item actives, potions and wards from the API item list (slot 0-5 inventory, 6 trinket)."""
    out = []
    for it in items:
        name = str(it.get("displayName", ""))
        low = name.lower()
        slot = int(it.get("slot", -1))
        if slot < 0:
            continue
        if slot == 6:
            bind_fn, ev = (lambda kb: kb.vision_item), "evtUseVisionItem"
        else:
            bind_fn, ev = (lambda kb, s=slot: kb.item(s + 1)), f"evtUseItem{slot + 1}"
        if any(p in low for p in POTIONS):
            if hp_pct < 85 and ctx.now - potion_used_at > 12:
                out.append(Spec(f"potion_slot{slot + 1}", f"Drink {name}: heal over time.", NONE, run=cast_run(bind_fn, ev)))
            continue
        usable = bool(it.get("canUse")) or "ward" in low or low in ITEM_TARGETS
        if not usable or not hud_ready.get(slot, True):
            continue
        tt, who, rng = ITEM_TARGETS.get(low, (NONE, "any", 0))
        if "oracle" in low:
            tt, who, rng = NONE, "any", 0
        key = "ward" if ("ward" in low or "farsight" in low) else f"item_slot{slot + 1}"
        text = f"Use {name}" + (": place a ward at the chosen point for vision." if key == "ward" else " (its active).")
        out.append(Spec(key, text, tt, who=who, range=rng, run=cast_run(bind_fn, ev)))
    return out


# -- resolution and execution -------------------------------------------------------------
def resolve_target(ctx: Ctx, spec: Spec, cands: dict[str, tuple[str, Any]], probs: dict[str, float] | None):
    """Most likely valid unit for this action by the target head, else the nearest valid one."""
    valid_labels = [l for l, (k, u) in cands.items() if valid(spec, k) and (spec.accept is None or spec.accept(k, u))]
    if not valid_labels:
        return None
    in_range = [l for l in valid_labels if dist_units(ctx.sc, cands[l][1]) <= spec.range + 400] or valid_labels
    if probs:
        best = max(in_range, key=lambda l: probs.get(l, 0.0))
        if probs.get(best, 0.0) > 0:
            return cands[best][1]
    return cands[min(in_range, key=lambda l: dist_units(ctx.sc, cands[l][1]))][1]


def resolve_point(ctx: Ctx, spec: Spec, cands, where_probs: dict[str, float] | None, target_probs, distance: float | None) -> tuple[float, float]:
    sc = ctx.sc
    mx, my = sc.me_xy
    order = sorted(WHERE, key=lambda w: -(where_probs or {}).get(w, 0.0)) if where_probs else ["at_target", "toward_enemy_tower"]
    level = 1 if distance is None else max(0, min(2, int(round(distance))))
    units = DISTANCES[level] or (spec.range or 600.0)
    units = min(units, spec.range or units)
    for w in order:
        if w == "at_target":
            tgt = resolve_target(ctx, spec, cands, target_probs)
            if tgt is None:
                continue
            return unit_xy(tgt)
        if w == "on_myself":
            return mx, my
        if w in ("toward_enemy_tower", "toward_my_tower"):
            fx, fy = ctx.lane.screen_dir(ctx.lane_progress) if ctx.lane is not None else ctx.mi.fwd
            s = 1 if w == "toward_enemy_tower" else -1
            return mx + s * fx * units * VC.px_per_unit, my + s * fy * units * VC.px_per_unit
        dx, dy = _dir(w)
        return mx + dx * units * VC.px_per_unit, my + dy * units * VC.px_per_unit
    return mx, my


def execute(ctx: Ctx, spec: Spec, cands, target_probs, where_probs, distance) -> str:
    """Run one chosen action. Returns a short description, or '' when it could not run."""
    if spec.mode:
        mi = ctx.mi
        # A committed all-in is not dropped for farming within 2.5 s (Jev re-picks ~5 times a
        # second and a flip-flop cancels the combo halfway); backing off is always allowed.
        if (mi.mode == "all_in" and spec.mode in ("farm", "push", "hold_with_carry") and ctx.sc.champ is not None
                and ctx.now - mi.mode_since < 2.5):
            return ""
        mi.set_mode(spec.mode, ctx.now)
        return f"mode {spec.mode}"
    tgt, pt = None, None
    if spec.target == UNIT:
        tgt = resolve_target(ctx, spec, cands, target_probs)
        if tgt is None:
            return ""
    elif spec.target == POINT:
        pt = resolve_point(ctx, spec, cands, where_probs, target_probs, distance)
    what = spec.run(ctx, spec, tgt, pt) if spec.run else ""
    if what:
        ctx.mi._ordered(ctx.now, what)
        ctx.mi.last_exec = {"name": spec.name, "what": what, "ts": ctx.now,
                            "target": ctx.mi._pt(*unit_xy(tgt)) if tgt is not None else None,
                            "point": ctx.mi._pt(*pt) if pt is not None else None}
    return what


# -- the question pack ---------------------------------------------------------------------
def questions(ctx: Ctx, specs: list[Spec], cands: dict[str, tuple[str, Any]], role_text: str, opp_name: str) -> dict:
    qs: dict[str, Any] = {
        "action": Choice(
            instructions=(f"You are {role_text}. Pick the single best move for the next half second, following "
                          "`plan_from_strategy` unless the situation clearly calls for something else. Targeted "
                          "moves use the `target` answer; ground moves use `where` and `distance`."),
            criteria={s.name: s.text for s in specs},
        ),
    }
    if cands and any(s.target == UNIT or s.target == POINT for s in specs):
        qs["target"] = Choice(
            instructions="If my move needs a unit (an attack, a targeted spell, or aiming at someone), which one?",
            criteria={l: describe(ctx, l, k, u, opp_name) for l, (k, u) in cands.items()},
        )
    if any(s.target == POINT for s in specs):
        qs["where"] = Choice(instructions="If my move needs a spot on the ground (walk, dash, skillshot, ward, flash), where?",
                             criteria=WHERE)
        qs["distance"] = Score(instructions="How far from me should that spot be?",
                               criteria=["Short: about 150 units", "Medium: about 350 units", "Long: the move's full range"])
    return qs
