"""Offline check of the fight layer on synthetic scenes with a dry-run controller: Yasuo's and
Lee Sin's combos step by step, trade windows (and their refusal into a full wave), the escape
dash, and the kill-window median. No game, no Jev call.
Run: uv run python tests/offline_fight_check.py"""
import time

from jev import config as _config
_config.FAST.lane_planner = False  # these check the rule path; tests/offline_laneplan_check.py checks the planner

from jev import config, keybinds
from jev.control import Controller
from jev.kits import LeeSin, Yasuo
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.vision import Unit

VC = config.VISION
ME = (862.0, 490.0)


def unit(kind: str, dx_units: float, dy_units: float, hp: float) -> Unit:
    x, y = ME[0] + dx_units * VC.px_per_unit, ME[1] + dy_units * VC.px_per_unit
    return Unit(kind, "enemy", x, y, hp, (int(x) - 40, int(y) - 80, 60, 10))


def track(u: Unit, now: float) -> Track:
    t = Track(id=id(u) % 10000, unit=u, seen=now)
    for k in range(4):
        t.hist.append((now - 0.1 * (3 - k), u.hp))
        t.path.append((now - 0.1 * (3 - k), u.x, u.y))
    return t


def scene(champ: Track | None, minions: list[Track], ready: str, r_lit: bool = False) -> Scene:
    sc = Scene(me_xy=ME, minions=minions)
    sc.champ = champ
    sc.champ_dist = sc.dist(champ) if champ else None
    sc.ready = {k: True for k in ready}
    sc.r_lit = r_lit
    return sc


def micro() -> tuple[Micro, list[str]]:
    log: list[str] = []
    mi = Micro(Controller(dry_run=True, log=log.append), Screen(), keybinds.load(), "ORDER")
    mi.summoners = ["flash", "ignite"]
    return mi, log


now = time.time()

# Yasuo trade: E onto the champion with Q during the dash.
y = Yasuo()
mi, log = micro()
mi.set_mode("trade", now)
sc = scene(track(unit("champion", 380, 0, 0.8), now), [], "QE")
assert y.step(mi, sc, now, 0.7, "trade", False)
print("yasuo trade:", mi.last_action)
assert "E+Q onto the champion" in mi.last_action

# Q3 tornado from range.
y = Yasuo()
y.q.stacks, y.q.last_gain = 2, now
mi, log = micro()
sc = scene(track(unit("champion", 800, 0, 0.8), now), [], "Q")
y.fight(mi, sc, now, 0.7, "all_in")
print("yasuo q3:", mi.last_action)
assert "Q3 tornado" in mi.last_action

# R on an airborne target comes first.
mi, log = micro()
sc = scene(track(unit("champion", 600, 0, 0.5), now), [], "QER", r_lit=True)
Yasuo().fight(mi, sc, now, 0.7, "all_in")
print("yasuo r:", mi.last_action)
assert "R Last Breath" in mi.last_action

# Ignite a low champion in melee range when the spells are down.
mi, log = micro()
sc = scene(track(unit("champion", 200, 0, 0.2), now), [], "DF")
Yasuo().fight(mi, sc, now, 0.7, "all_in")
print("yasuo ignite:", mi.last_action)
assert "ignite" in mi.last_action

# No dash into a champion standing in her full wave (unless she is low).
crowd = [track(unit("minion", 380 + dx, dy, 0.9), now) for dx, dy in ((60, 30), (-40, 60), (30, -60), (90, 0))]
mi, log = micro()
sc = scene(track(unit("champion", 380, 0, 0.8), now), crowd, "QE")
Yasuo().fight(mi, sc, now, 0.7, "trade")
print("yasuo into the wave:", mi.last_action)
assert "E+Q" not in mi.last_action and "E onto" not in mi.last_action
mi.hp_pct = 90
assert Yasuo().trade_window(mi, sc, now, 0) is None
sc_open = scene(track(unit("champion", 380, 0, 0.8), now), [], "QE")
assert Yasuo().trade_window(mi, sc_open, now, 0) == "trade"
assert Yasuo().trade_window(mi, sc_open, now, -1) is None  # behind in levels: no trade

# Wind wall: losing HP fast to a champion out of melee range.
mi, log = micro()
mi.hp_lost = 15
sc = scene(track(unit("champion", 600, 0, 0.9), now), [], "W")
Yasuo().fight(mi, sc, now, 0.7, "trade")
print("yasuo wall:", mi.last_action)
assert "W wind wall" in mi.last_action

# Escape: hurt, enemy close, a minion toward home (down-left on screen for the blue side).
mi, log = micro()
mi.hp_pct = 25
home = (-0.707, 0.707)
sc = scene(track(unit("champion", 300, -100, 0.9), now), [track(unit("minion", -300, 300, 0.8), now)], "E")
assert Yasuo().escape(mi, sc, now, home)
print("yasuo escape:", mi.last_action)
sc_bad = scene(track(unit("champion", 300, -100, 0.9), now), [track(unit("minion", 300, -300, 0.8), now)], "E")
assert not Yasuo().escape(mi, sc_bad, now, home)  # the only minion is toward the enemy

# Lee Sin gank: Q1 from range, Q2 after a hit, Flurry autos, E, R to finish.
lee = LeeSin("JUNGLE")
mi, log = micro()
sc = scene(track(unit("champion", 800, 0, 0.9), now), [], "QWE")
lee.fight(mi, sc, now, 0.7, "all_in")
print("lee q1:", mi.last_action)
assert "Sonic Wave" in mi.last_action
t = now + 0.5  # Q re-lit: the wave hit
mi.last_order = 0
lee.fight(mi, sc, t, 0.7, "all_in")
print("lee q2:", mi.last_action)
assert "Q2 dash" in mi.last_action
t += 0.3
mi.last_order = 0
sc = scene(track(unit("champion", 150, 0, 0.7), t), [], "E")
lee.fight(mi, sc, t, 0.7, "all_in")
print("lee after q2:", mi.last_action)
assert "flurry auto" in mi.last_action
t += 2.0
mi.last_order, mi.last_attack = 0, 0
lee.autos_since = 2
lee.fight(mi, sc, t, 0.7, "all_in")
print("lee e:", mi.last_action)
assert "E Tempest" in mi.last_action
t += 0.2
mi.last_order = 0
sc = scene(track(unit("champion", 200, 0, 0.2), t), [], "R")
lee.fight(mi, sc, t, 0.7, "all_in")
print("lee r:", mi.last_action)
assert "R kick" in mi.last_action
mi.hp_pct = 80
assert LeeSin("JUNGLE").trade_window(mi, scene(track(unit("champion", 800, 0, 0.9), now), [], "Q"), now, 0) == "all_in"

# Trades wind down: after the burst, step back, then farm again.
y = Yasuo()
mi, log = micro()
mi.hp_pct = 75.0  # not ahead of her 80%: one auto after the burst, then step back
mi.set_mode("trade", now)
y.burst_at = now + 0.01
sc = scene(track(unit("champion", 380, 0, 0.8), now), [], "")
y.step(mi, sc, now + 1.0, 0.7, "trade", False)
print("trade wind-down:", mi.last_action)
assert mi.last_action == "trade: step back"
# Ahead of her by 10+ and she is not in her wave: still trading a second after the burst.
y2 = Yasuo()
mi2, _ = micro()
mi2.hp_pct = 95.0
mi2.set_mode("trade", now)
y2.burst_at = now + 0.01
y2.step(mi2, scene(track(unit("champion", 380, 0, 0.8), now), [], ""), now + 1.0, 0.7, "trade", False)
assert mi2.last_action != "trade: step back", mi2.last_action
y.step(mi, sc, now + 3.0, 0.7, "trade", False)
assert mi.mode == "farm"
print("FIGHT OK")

# Q last hit that would also hit the champion (and turn her wave on me): auto instead.
y = Yasuo()
mi, log = micro()
wave = [track(unit("minion", 250, 0, 0.08), now)] + [track(unit("minion", 350 + 20 * k, 60 * k - 60, 0.9), now) for k in range(3)]
sc = scene(track(unit("champion", 420, 10, 0.9), now), wave, "Q")
sc.killable_q = [wave[0]]
y.continuous(mi, sc, now, 0.7, "farm", False)
print("q through the champion:", mi.last_action)
assert mi.last_action != "Q last hit"
sc = scene(track(unit("champion", -100, 400, 0.9), now), wave, "Q")
sc.killable_q = [wave[0]]
mi.last_order = 0
y.continuous(mi, sc, now, 0.7, "farm", False)
assert mi.last_action == "Q last hit", mi.last_action
print("FIGHT OK (q line)")

# Q3 tornado: poke at the champion in range (not a minion last hit), then R on the knock-up goes all in.
y = Yasuo()
y.q.stacks, y.q.last_gain = 2, now
mi, log = micro()
mi.hp_pct = 80
sc = scene(track(unit("champion", 800, 0, 0.6), now), [], "Q")
assert y.reflex(mi, sc, now, {})
print("q3 poke:", mi.last_action)
assert "Q3 tornado at the champion" in mi.last_action
mi.last_order = 0
sc = scene(track(unit("champion", 700, 0, 0.5), now + 0.4), [], "R", r_lit=True)
assert y.reflex(mi, sc, now + 0.4, {})
assert "R reflex" in mi.last_action and mi.mode == "all_in", (mi.last_action, mi.mode)
print("FIGHT OK (q3 poke)")

# Pushing with no champion in view: Q into the wave.
y = Yasuo()
mi, log = micro()
wave = [track(unit("minion", 300 + 30 * k, 20 * k, 0.9 - 0.1 * k), now) for k in range(4)]
sc = scene(None, wave, "Q")
assert y.continuous(mi, sc, now, 0.7, "farm", True)
print("push:", mi.last_action)
assert mi.last_action.startswith("push: Q")
print("FIGHT OK (push)")

# A move just before a minion becomes killable must not block the last hit (the order gate, g29).
mi, log = micro()
mi.fwd = (1.0, 0.0)
mi.move_screen(ME[0] - 50, ME[1], now, "farm: hold behind the wave", every=0.0)
low = track(unit("minion", 150, 0, 0.1), now)
sc = scene(None, [low], "")
sc.killable_auto = [low]
assert mi._can_order(now + 0.01), "a move closed the order gate"
mi.farm_step(sc, now + 0.01, 0.7)
print("after a move:", mi.last_action)
assert mi.last_action.startswith("last hit"), mi.last_action
print("FIGHT OK (order gate)")

# E last hit: a minion 350 units away that the dash kills, the auto on cooldown; another minion
# next to the landing spot takes the circle Q (a stack).
y = Yasuo()
y.q.hud = False
mi, log = micro()
mi.hp_pct = 90.0
mi.last_attack = now - 0.5  # auto on cooldown, past its wind-up
low = track(unit("minion", 350, 0, 0.15), now)
other = track(unit("minion", 480, 40, 0.9), now)
sc = scene(None, [low, other], "QE")
sc.allies = 3
sc.killable_e = [low]
assert y.continuous(mi, sc, now + 0.05, 0.7, "farm", False)
print("E last hit:", mi.last_action, "| queued:", len(mi._later))
assert mi.last_action == "E+Q last hit" and len(mi._later) == 1
# The dash would land next to a healthier champion: no E.
y = Yasuo()
y.q.hud = False
mi, log = micro()
mi.hp_pct = 50.0
mi.last_attack = now - 0.5
ch = track(unit("champion", 800, 0, 0.9), now)
sc = scene(ch, [low], "E")
sc.allies = 3
sc.killable_e = [low]
y.continuous(mi, sc, now + 0.05, 0.7, "farm", False)
print("E last hit toward a healthier champion:", mi.last_action)
assert "E" not in mi.last_action.split(" ")[0]
print("FIGHT OK (E last hit)")

# Fight gap-close through a minion that lands on her: Q in the dash.
y = Yasuo()
y.q.hud = False
mi, log = micro()
mi.hp_pct = 90.0
mi.set_mode("all_in", now)
ch = track(unit("champion", 650, 0, 0.6), now)
m = track(unit("minion", 300, 0, 0.9), now)
sc = scene(ch, [m], "QE")
sc.dash_options = [(m, 450.0)]
y.fight(mi, sc, now, 0.7, "all_in")
print("gap close:", mi.last_action, "| queued:", len(mi._later))
assert mi.last_action == "all_in: E+Q through a minion toward the champion" and len(mi._later) == 1
print("FIGHT OK (gap close EQ)")

# Level-ups follow the kit's order: E at 2, R at 6/11/16; g29's level-6 state (Jev had spent the
# points on W and E) takes R; a capped pick falls through to the next legal one.
from jev.brain import legal_level_ups
from jev.mechanics import Mechanics
m = Mechanics(Controller(dry_run=True, log=lambda s: None), Screen(), keybinds.load(), "ORDER", skill_order=Yasuo().skill_order)
lv, seq = {"Q": 0, "W": 0, "E": 0, "R": 0}, ""
for level in range(1, 19):
    ab = m.level_up(lv, legal_level_ups(level, lv))
    lv[ab] += 1
    seq += ab
print("Yasuo levels:", seq)
assert seq[1] == "E" and seq[5] == seq[10] == seq[15] == "R"
assert m.level_up({"Q": 3, "W": 1, "E": 1, "R": 0}, legal_level_ups(6, {"Q": 3, "W": 1, "E": 1, "R": 0})) == "R"
assert m.level_up({"Q": 2, "W": 1, "E": 0, "R": 0}, legal_level_ups(4, {"Q": 2, "W": 1, "E": 0, "R": 0})) == "E"
print("FIGHT OK (level-ups)")

# An all in she walks out of: 2.5 s out of reach at 60% ends it; at 20% the chase goes on.
y = Yasuo()
y.q.hud = False
for hp_her, want_farm in ((0.6, True), (0.2, False)):
    mi, log = micro()
    mi.set_mode("all_in", now)
    ch = track(unit("champion", 900, 0, hp_her), now)
    sc = scene(ch, [], "")
    y._in_reach_t = now
    for k in range(4):
        mi.last_order = 0
        y.hit_or_chase(mi, sc, now + 1.0 * k, 0.7, "all_in")
    print(f"all in, she is at {hp_her:.0%} and out of reach 3 s:", mi.mode, "|", mi.last_action)
    assert (mi.mode == "farm") == want_farm, mi.mode
print("FIGHT OK (all-in chase)")

# Mechanics from the wiki: E through her lands 475 from where I start, so from 150 units the E+Q
# circle (215) misses her: Q her there instead. From 350 the E+Q lands on her.
y = Yasuo()
y.q.hud = False
for dist_u, want in ((150, "Q the champion"), (350, "E+Q onto the champion")):
    mi, log = micro()
    mi.hp_pct = 90.0
    mi.set_mode("all_in", now)
    sc = scene(track(unit("champion", dist_u, 0, 0.6), now), [], "QE")
    y.fight(mi, sc, now, 0.7, "all_in")
    print(f"all in at {dist_u}u:", mi.last_action)
    assert want in mi.last_action, mi.last_action
# No spell in an auto's swing: the auto lands first.
mi, log = micro()
mi.set_mode("all_in", now)
mi.last_attack = now - 0.05
sc = scene(track(unit("champion", 150, 0, 0.6), now), [], "QE")
y.fight(mi, sc, now, 0.7, "all_in")
print("in the swing:", mi.last_action or "(waits)")
assert "Q the champion" not in mi.last_action
# The tornado lead: cast plus flight at 1200 u/s.
from jev.kits import tornado_lead
print(f"tornado lead at 900u: {tornado_lead(900):.2f} s")
assert abs(tornado_lead(900) - (config.FAST.q_cast_s + 0.75)) < 1e-6
# R on an ally's knock-up in an even fight (no tornado of mine).
y = Yasuo()
mi, log = micro()
mi.hp_pct = 70.0
sc = scene(track(unit("champion", 600, 0, 0.6), now), [], "R", r_lit=True)
sc.ally_champs, sc.enemy_champs = [Unit("champion", "ally", ME[0] + 300, ME[1], 0.8, (0, 0, 1, 1))], 1
assert y.reflex(mi, sc, now, {"fight_favorable": 0.5})
print("ally knock-up:", mi.last_action)
assert "ally" in mi.last_action
print("FIGHT OK (mechanics)")

# Tornado angle: two of them in a line to the right and one up alone: the line through two is thrown
# (the focus alone only on a tie). Q's cast shortens with attack speed (0.35 s -> 0.175 s).
from jev.kits import tornado_aim, q_cast_time
a = track(unit("champion", 600, 0, 0.8), now)
b = track(unit("champion", 900, 40, 0.8), now)
c = track(unit("champion", 0, -700, 0.8), now)
a.id, b.id, c.id = 101, 102, 103
sc = scene(c, [], "Q")
sc.champs = [a, b, c]
x, y = tornado_aim(sc, c, now, 0.7)
print("tornado aim:", (round(x - ME[0]), round(y - ME[1])), "(right = the pair, up = the focus alone)")
assert x - ME[0] > 200 and abs(y - ME[1]) < 100
sc.champs = [c]
x, y = tornado_aim(sc, c, now, 0.7)
assert y - ME[1] < -200
print(f"Q cast: {q_cast_time(0.697):.3f} s at base attack speed, {q_cast_time(1.4):.3f} s at 1.4")
assert abs(q_cast_time(0.697) - 0.35) < 1e-3 and q_cast_time(1.4) < 0.2
print("FIGHT OK (tornado aim)")

# Beyblade: all in with the kill on (her at 30%, me healthy, R up, Flash up), her 800 units away, a
# minion 400 units toward her: E through it, Q3 in the dash, Flash onto her. Healthy her: the tornado.
for her_hp, want in ((0.3, "beyblade"), (0.8, "Q3 tornado")):
    y = Yasuo()
    y.q.hud = True
    y.r_rank = 1
    mi, log = micro()
    mi.hp_pct = 85.0
    mi.summoners = ["flash", "ignite"]
    mi.set_mode("all_in", now)
    her = track(unit("champion", 800, 0, her_hp), now)
    gate = track(unit("minion", 400, 0, 0.9), now)
    sc = scene(her, [gate], "QED")
    y.fight(mi, sc, now, 0.7, "all_in")
    print(f"all in, her at {her_hp:.0%} 800u away:", mi.last_action, "| queued", len(mi._later))
    assert want in mi.last_action, mi.last_action
print("FIGHT OK (beyblade)")

# Lee's Sonic Wave stops at the first unit: a minion in the line to her holds the Q; a clear line throws it.
lee = LeeSin()
for block, want_q in ((True, False), (False, True)):
    mi, log = micro()
    mi.set_mode("all_in", now)
    her = track(unit("champion", 800, 0, 0.7), now)
    mins = [track(unit("minion", 400, 30, 0.9), now)] if block else [track(unit("minion", 400, 400, 0.9), now)]
    sc = scene(her, mins, "Q")
    lee.q_at = -10.0
    lee.fight(mi, sc, now, 0.7, "all_in")
    print("Sonic Wave, minion in the line" if block else "Sonic Wave, line clear", "->", mi.last_action)
    assert ("Sonic Wave" in mi.last_action) == want_q, mi.last_action
print("FIGHT OK (sonic wave line)")
