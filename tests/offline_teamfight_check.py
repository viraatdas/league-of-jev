"""Fight entries without Jev (credits out, stale reads): join a team fight when allied champions
are on screen and the numbers are even, all in on a lane opponent who is low while I am healthy,
and focus the weakest champion in reach rather than the nearest. Dry-run player, no game.
Run: uv run python tests/offline_teamfight_check.py"""
import time

from jev import config, keybinds
from jev.control import Controller
from jev.loop import Player
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.vision import Unit

ME = (862.0, 490.0)
PPU = config.VISION.px_per_unit


def track(dx: float, hp: float, tid: int, now: float, team: str = "enemy") -> Track:
    u = Unit("champion", team, ME[0] + dx * PPU, ME[1], hp, (0, 0, 10, 10))
    t = Track(id=tid, unit=u, seen=now)
    for k in range(5):
        t.hist.append((now - 0.1 * (4 - k), hp))
    return t


def setup(now: float):
    p = Player(dry_run=True)
    p.micro = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
    p.fights = None
    p.micro.hp_pct = 80.0
    return p


now = time.time()
# Team fight: one ally on screen, one enemy at 70%: all in.
p = setup(now)
e = track(500, 0.7, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME, ally_champs=[Unit("champion", "ally", ME[0] - 50, ME[1], 0.8, (0, 0, 1, 1))])
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
p._fight_triggers(sc, p.micro, now)
print("team fight:", p.micro.mode, list(p.log_lines)[-1:])
assert p.micro.mode == "all_in"

# Alone against a healthy enemy: no entry.
p = setup(now)
e = track(500, 0.9, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
p._fight_triggers(sc, p.micro, now)
assert p.micro.mode != "all_in", p.micro.mode

# Kill pressure: she is at 35%, I am at 80%, no wave around her.
p = setup(now)
e = track(550, 0.35, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
p._fight_triggers(sc, p.micro, now)
print("kill pressure:", p.micro.mode, list(p.log_lines)[-1:])
assert p.micro.mode == "all_in"

# Focus: nearest at 90%, another in reach at 30%: the weak one is the target, and it sticks.
p = setup(now)
near, weak = track(300, 0.9, 1, now), track(650, 0.3, 2, now)
p.champ_tracker.tracks = {1: near, 2: weak}
sc = Scene(me_xy=ME, ally_champs=[Unit("champion", "ally", ME[0] - 50, ME[1], 0.8, (0, 0, 1, 1))])
sc.champ, sc.champ_dist, sc.enemy_champs = near, sc.dist(near), 2
p._fight_triggers(sc, p.micro, now)
print("focus:", sc.champ.id, p.micro.mode, list(p.log_lines)[-1:])
assert sc.champ is weak and p.micro.mode == "all_in"
weak.unit.hp = 0.6  # a misread next frame: still the committed target
sc2 = Scene(me_xy=ME, ally_champs=sc.ally_champs)
sc2.champ, sc2.champ_dist, sc2.enemy_champs = near, sc2.dist(near), 2
p._fight_triggers(sc2, p.micro, now + 0.2)
assert sc2.champ is weak

# Sidestep: holding behind the wave with their champion in skillshot range, the hold point alternates.
mi = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
mi.fwd = (1.0, 0.0)
m = track(300, 0.9, 5, now)
m.unit.kind = "minion"
sc = Scene(me_xy=ME, minions=[m])
c = track(900, 0.9, 6, now)
sc.champ, sc.champ_dist = c, sc.dist(c)
sides = set()
for k in range(6):
    mi.last_order = 0
    mi.farm_step(sc, now + k * 1.0, 0.7)
    sides.add(mi._juke_side)
print("sidestep:", mi.last_action, sides)
assert mi.last_action.startswith("farm: sidestep") and sides == {1, -1}
print("TEAMFIGHT OK")
