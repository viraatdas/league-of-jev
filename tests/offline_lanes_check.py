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
