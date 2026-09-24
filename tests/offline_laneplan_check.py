"""Yasuo's lane planner on synthetic scenes: a sure auto last hit beats everything; E through a
minion the dash kills when the auto is on cooldown, with Q in the dash onto a second minion; no
dash that lands next to a healthier champion; the tornado kept for her and thrown when she is in
reach; no wandering (hold) when nothing is worth doing; out of their wave with none of ours.
Run: uv run python tests/offline_laneplan_check.py"""
import time

from jev import config, keybinds
from jev.control import Controller
from jev.kits import Yasuo
from jev.laneplan import LanePlanner, p_kill
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.vision import Unit

VC = config.VISION
ME = (862.0, 490.0)
PPU = VC.px_per_unit
now = time.time()


def tr(kind, dx, dy, hp, tid, hp_max=300.0, role="caster", rate=0.0):
    u = Unit(kind, "enemy", ME[0] + dx * PPU, ME[1] + dy * PPU, hp, (0, 0, 60, 4))
    t = Track(id=tid, unit=u, seen=now)
    for k in range(5):
        t.hist.append((now - 0.1 * (4 - k), hp + rate * 0.1 * (4 - k)))
    t.hp_max, t.role, t.ally_takes = hp_max, role, False
    t.unit.hp = hp
    return t


def scene(minions, champ=None, ready="QE", allies=3, ad=70.0):
    sc = Scene(me_xy=ME, minions=minions, allies=allies, ad=ad, q_dmg=0.88 * (20 + 1.05 * ad), e_dmg=0.85 * 60)
    sc.ally_units = [Unit("minion", "ally", ME[0] + 150 * PPU, ME[1], 0.9, (0, 0, 60, 4)) for _ in range(allies)]
    sc.champ = champ
    sc.champ_dist = sc.dist(champ) if champ else None
    sc.enemy_champs = 1 if champ else 0
    sc.ready = {k: True for k in ready}
    sc.lane = {"opp_range": 125.0, "opp_hp": 700.0, "aggression": 1.0}
    return sc


def micro():
    mi = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
    mi.fwd, mi.hp_pct = (1.0, 0.0), 90.0
    return mi


assert p_kill(70, 40, 300) > 0.9 and p_kill(70, 100, 300) < 0.1 and p_kill(70, 0, 300) == 0.0
y = Yasuo()
y.q.hud = False
pl = LanePlanner(y)

# A sure auto last hit in reach: take it.
mi = micro()
low = tr("minion", 200, 0, 0.15, 1)
best, top = pl.choose(scene([low, tr("minion", 260, 30, 0.9, 2)]), mi, now, False)
print("sure auto:", best.kind, best.why, [(o.kind, round(o.value, 1)) for o in top[1:]])
assert best.kind == "auto" and best.target is low

# Auto on cooldown, Q down, a minion at 350 units the E kills: E it (the old rules walked to it).
mi = micro()
mi.last_attack = now - 0.5
low = tr("minion", 350, 0, 0.10, 1)
best, top = pl.choose(scene([low, tr("minion", 480, 30, 0.9, 2)], ready="E"), mi, now, False)
print("E last hit:", best.kind, best.why, [(o.kind, round(o.value, 1)) for o in top[1:]])
assert best.kind == "e" and best.target is low
# Q up too, the E kill sure and a second minion in the circle about to die: E+Q takes both.
mi = micro()
mi.last_attack = now - 0.5
two = tr("minion", 490, 20, 0.2, 2, rate=0.25)
best, top = pl.choose(scene([low, two]), mi, now, False)
print("E+Q two last hits:", best.kind, best.why, [(o.kind, round(o.value, 1)) for o in top[1:]])
assert best.kind == "eq" and best.target is low

# Same, but the landing is next to a healthier champion (me 40%, her 90%): no dash.
mi = micro()
mi.hp_pct, mi.last_attack = 40.0, now - 0.5
her = tr("champion", 650, 0, 0.9, 9, role="")
best, top = pl.choose(scene([low, tr("minion", 480, 30, 0.9, 2)], champ=her), mi, now, False)
print("dash toward her:", best.kind, best.why)
assert best.kind not in ("e", "eq")

# Nothing worth doing (full-HP wave, auto on cooldown, Q down): hold still rather than wander.
mi = micro()
mi.last_attack = now - 0.5
far = [tr("minion", 330 + 20 * k, 10 * k, 1.0, 10 + k) for k in range(3)]
best, top = pl.choose(scene(far, ready=""), mi, now, False)
print("nothing to do:", best.kind, best.why)
assert best.kind == "hold"

# The tornado up, their wave in reach, she is not: no Q3 on the wave. She walks into reach: tornado her.
y.q.hud = True
mi = micro()
mi.last_attack = now - 0.5
best, top = pl.choose(scene(far, ready="Q"), mi, now, False)
print("tornado, no champion:", best.kind)
assert best.kind != "q"
her = tr("champion", 700, 0, 0.8, 9, role="")
best, top = pl.choose(scene(far, champ=her, ready="Q"), mi, now, False)
print("tornado, she is in reach:", best.kind, best.why)
assert best.kind == "q3_champ"
y.q.hud = False

# Standing past their front line with none of ours: get out.
mi = micro()
mi.last_attack = now - 0.5
wave = [tr("minion", -150 + 40 * k, 20 * k, 0.9, 20 + k) for k in range(4)]
sc = scene(wave, ready="", allies=0)
sc.ally_units = []
best, top = pl.choose(sc, mi, now, False)
print("in their wave alone:", best.kind, best.why)
assert best.kind == "move" and best.point[0] < ME[0]

# The executor: the kit carries out the pick and logs it.
y = Yasuo()
y.q.hud = False
mi = micro()
mi.last_attack = now - 0.5
low = tr("minion", 350, 0, 0.10, 1)
sc = scene([low, tr("minion", 480, 30, 0.9, 2)], ready="E")
assert y.continuous(mi, sc, now, 0.7, "farm", False)
print("executed:", mi.last_action, "| logged:", mi.plan_log[-1]["pick"])
assert mi.last_action.startswith("E") and mi.plan_log
print("LANEPLAN OK")
