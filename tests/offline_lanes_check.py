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
print("LANES OK")
