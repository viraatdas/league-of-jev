"""Offline check of the fight layer on synthetic scenes with a dry-run controller: Yasuo's and
Lee Sin's combos step by step, trade windows (and their refusal into a full wave), the escape
dash, and the kill-window median. No game, no Jev call.
Run: uv run python tests/offline_fight_check.py"""
import time

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
mi.set_mode("trade", now)
y.burst_at = now + 0.01
sc = scene(track(unit("champion", 380, 0, 0.8), now), [], "")
y.step(mi, sc, now + 1.0, 0.7, "trade", False)
print("trade wind-down:", mi.last_action)
assert mi.last_action == "trade: step back"
y.step(mi, sc, now + 3.0, 0.7, "trade", False)
assert mi.mode == "farm"
print("FIGHT OK")
