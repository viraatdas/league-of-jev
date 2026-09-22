"""Named places on Summoner's Rift for the strategy head's `destination` answer (map units,
approximate). "my_" / "enemy_" are resolved per side. Travel is a minimap click; the game's
pathfinder does the rest."""
from __future__ import annotations

import math

from jev import config
from jev.lanes import Lane

BT, RT = config.BLUE_TOWERS, config.RED_TOWERS
NEUTRAL = {
    "dragon_pit": ((9866.0, 4414.0), "dragon and the drakes"),
    "baron_pit": ((5007.0, 10471.0), "Baron Nashor (and Rift Herald / Voidgrubs early)"),
    "top_river": ((5800.0, 9200.0), "river between mid and the baron pit"),
    "bot_river": ((9000.0, 5600.0), "river between mid and the dragon pit"),
}
SIDED = {  # blue-side coordinates; red side uses the mirrored entry
    "blue_buff": ((3872.0, 7900.0), (10931.0, 6990.0)),
    "red_buff": ((7862.0, 4111.0), (7016.0, 10775.0)),
    "top_tower": (BT[0], RT[0]),
    "mid_tower": (BT[3], RT[3]),
    "bot_tower": (BT[6], RT[6]),
    "base": (config.BLUE_FOUNTAIN, config.RED_FOUNTAIN),
}


def places(side: str) -> dict[str, tuple[tuple[float, float], str]]:
    """name -> (map point, what is there) for this side."""
    mine = 0 if side == "ORDER" else 1
    out: dict[str, tuple[tuple[float, float], str]] = {}
    for lane in ("top", "mid", "bot"):
        ln = Lane(lane, side)
        out[f"{lane}_lane"] = (ln.point(ln.center), f"the middle of {lane} lane (farm or fight there)")
    out.update(NEUTRAL)
    for key, pts in SIDED.items():
        what = key.replace("_", " ")
        out[f"my_{key}"] = (pts[mine], f"my {what}")
        out[f"enemy_{key}"] = (pts[1 - mine], f"the enemy {what}")
    return out


def nearest(pos: tuple[float, float], side: str) -> str:
    return min(places(side).items(), key=lambda kv: math.dist(kv[1][0], pos))[0]


def describe(side: str, me: tuple[float, float] | None, enemies: list, allies: list, timers: dict) -> dict[str, str]:
    """Criteria text for the destination question: distance, and who was last seen near it."""
    out = {}
    for name, (pt, what) in places(side).items():
        bits = [what]
        if me is not None:
            bits.append(f"{int(round(math.dist(me, pt) / 100) * 100)} units away")
        ne = sum(1 for e in enemies if math.dist(e, pt) < 2500)
        na = sum(1 for a in allies if math.dist(a, pt) < 2500)
        if ne:
            bits.append(f"{ne} enemy champion(s) seen near it")
        if na:
            bits.append(f"{na} allied champion(s) near it")
        if name == "dragon_pit" and timers.get("next_dragon_in_s") is not None:
            bits.append(f"dragon spawns in {timers['next_dragon_in_s']} s" if timers["next_dragon_in_s"] else "dragon is up")
        if name == "baron_pit" and timers.get("next_baron_in_s") is not None:
            bits.append(f"baron spawns in {timers['next_baron_in_s']} s" if timers["next_baron_in_s"] else "baron is up")
        out[name] = "; ".join(bits)
    return out
