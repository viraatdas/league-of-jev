"""Thresh support walks the bot lane polyline (not the mid diagonal) and levels in his order."""
import time

from jev.kits import Thresh
from jev.lanes import Lane
from jev.loop import Player
from jev.mechanics import Mechanics
from jev.riot_api import load_fixture
from jev.brain import Decision

p = Player(dry_run=True, champion="thresh")
p.kit = Thresh()
p.lane = Lane("bot", "ORDER")
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane, skill_order=p.kit.skill_order)
data = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
data["activePlayer"]["championStats"]["moveSpeed"] = 370.0
data["activePlayer"]["championStats"]["currentHealth"] = 1300.0
p.decision = Decision("farm", 0.6, {"farm": 0.6}, 0.8, 0.1, 0.5, 1.0, None, 0.0, 150, "jev-test", 1400)
now = time.time()
for i in range(200):
    p._tick(data, now + i * 0.2)
x, y = p.lane.point(p.mech.nav.progress)
print("phase", p.phase, "progress", round(p.mech.nav.progress, 3), "own tower", round(p.lane.own_tower, 3), "map point", (int(x), int(y)))
assert p.phase == "lane" and y < 3000 and x > 5000, (x, y)  # on the bottom edge of the map, not the diagonal
print("last", p.mech.last_action, "| skill order", p.kit.skill_order[:6])
# go_to: Jev sends the support to mid; on arrival the lane switches and play continues there.
import time as _t
from jev.minimap import MinimapState
from jev.places import places
t = now + 60
p.decision = Decision("go_to", 0.6, {"go_to": 0.6}, 0.8, 0.1, 0.5, 1.0, None, 0.0, 150, "jev-test", 1400,
                      destination="mid_lane", ts=t)  # fresh: decisions older than 6 s fall back to rules
p.guards = type(p.guards)()
from jev.kits import Yasuo
p.kit = Yasuo()  # supports stay with their carry; travel is tested with a laner
p._tick(data, t)
print("go_to while away:", p.intent, "|", p.mech.last_action)
assert p.intent == "go_to" and "travel" in p.mech.last_action
p.mm_state = MinimapState(self_pos=places("ORDER")["mid_lane"][0], ts=_t.time())
p._tick(data, t + 2)
print("arrived:", p.lane.name, "|", p.log_lines[-1])
assert p.lane.name == "mid"
print("LANES OK")

# Our outer mid tower falls: retreats go behind the inner one.
from jev.loop import Player as _P
p = _P(dry_run=True)
p.side = "ORDER"
p._switch_lane("mid")
before = p.lane.own_tower
p._dead_turrets = {"Turret_T1_C_05_A"}
p._rehome_own_tower()
print("own tower", round(before, 3), "->", round(p.lane.own_tower, 3))
assert p.lane.own_tower < before - 0.05
print("LANES OK (towers)")

# Top lane: Yasuo south of the corner, the wave ~1000 units above him on the horizontal part. The
# lane direction is read where the minions are ("right"), not at his spot ("up"), g28/g29.
from jev.vision import Unit, View
from jev import config as _cfg
p = _P(dry_run=True)
p.side = "ORDER"
p._switch_lane("top")
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane, skill_order=Yasuo().skill_order)
me_pos = (1900.0, 12300.0)
p.mech.nav.progress = p.lane.project(me_pos)[0]
ppu = _cfg.VISION.px_per_unit
me = Unit("champion", "self", 860.0, 500.0, 1.0, (830, 400, 100, 10))
mins = [Unit("minion", t, 860.0 + dx, 500.0 - 1000 * ppu, 0.8, (0, 0, 60, 4))
        for t, dx in [("ally", -120), ("ally", -80), ("enemy", 60), ("enemy", 110)]]
v = View(units=mins + [me], me=me, ts=_t.time())
world = lambda u: (me_pos[0] + (u.x - me.x) / ppu, me_pos[1] - (u.y - me.y) / ppu)
at_me = p.lane.screen_dir(p.mech.nav.progress)
f = p._wave_fwd(v, v.enemies("minion"), world)
print("top corner: lane dir at me", tuple(round(c, 2) for c in at_me), "at the wave", tuple(round(c, 2) for c in f))
assert at_me[1] < -0.8 and f[0] > 0.8, (at_me, f)
assert p._wave_fwd(v, v.enemies("minion"), None) == at_me  # no position: the lane at our own spot
print("LANES OK (wave direction)")

# Recall: not with an enemy champion 600 units away on screen; with nothing near, yes.
from jev.vision import Unit as _U, View as _V
p = _P(dry_run=True)
me = _U("champion", "self", 862.0, 490.0, 0.9, (830, 400, 100, 10))
nasus = _U("champion", "enemy", 862.0 + 600 * ppu, 490.0, 0.8, (0, 0, 100, 10))
p.view = _V(units=[nasus, me], me=me, ts=_t.time())
assert p._recall_threat(_t.time())
p.view = _V(units=[me], me=me, ts=_t.time())
assert not p._recall_threat(_t.time())
print("LANES OK (recall spot)")
