"""Lee Sin jungler in dry run: leaves base toward the first camp on the route (red buff on the
blue side), and marks a camp cleared after standing there 20 s with no monsters in sight
(someone else took it)."""
import time

from jev.brain import Decision
from jev.kits import LeeSin
from jev.jungle import JungleState, camps
from jev.lanes import Lane
from jev.loop import Player
from jev.mechanics import Mechanics
from jev.minimap import MinimapState
from jev.riot_api import load_fixture

p = Player(dry_run=True, champion="leesin", role="JUNGLE")
p.kit = LeeSin("JUNGLE")
p.side = "ORDER"
p.lane = Lane("mid", "ORDER")
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane, skill_order=p.kit.skill_order)
p.jungle_state = JungleState("ORDER")
data = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
data["gameData"]["gameTime"] = 100.0
p.decision = Decision("farm", 0.6, {"farm": 0.6}, 0.5, 0.1, 0.5, 1.0, None, 0.0, 150, "jev-test", 1400, ts=time.time())
p._base_shop_done = True
now = time.time()
p.mm_state = MinimapState(self_pos=(3000.0, 3000.0), ts=time.time())
p._tick(data, now)
print("heading to:", p.jungle_state.current, "|", p.mech.last_action)
assert p.jungle_state.current == "red" and "jungle: to red" in p.mech.last_action
red = camps("ORDER")["red"]
p.mm_state = MinimapState(self_pos=red, ts=time.time())
for i in range(1, 24):
    data["gameData"]["gameTime"] = 100.0 + i
    p.mm_state.ts = time.time()
    p._tick(data, now + i)
print("cleared:", p.jungle_state.cleared, "next:", p.jungle_state.next_camp(red, 124.0))
assert "red" in p.jungle_state.cleared and p.jungle_state.next_camp(red, 124.0) == "krugs"
print("JUNGLE OK")

# A gank through the whole tick: level 4 at 5:00, their top laner pushed past the middle with ours there.
top = Lane("top", "ORDER")
data["gameData"]["gameTime"] = 300.0
data["activePlayer"]["level"] = 4
cs = data["activePlayer"]["championStats"]
cs["currentHealth"] = cs["maxHealth"]  # the fixture sits at 42%: ganks need 60%
p.mm_state = MinimapState(self_pos=camps("ORDER")["gromp"], ts=time.time(),
                          enemy_champions=[top.point(top.center - top.frac(900))],
                          ally_champions=[top.point(top.center - top.frac(1300))])
for i in range(3):
    p.mm_state.ts = time.time()
    p.state = p._full_state(data, p._tick(data, now + 40 + i))  # as the live loop does after each tick
print("gank tick:", p.intent, getattr(p, "_obj_label", None), "|", p.mech.last_action)
assert p.intent == "objective" and getattr(p, "_obj_label", "") == "gank top", (p.intent, getattr(p, "_obj_label", None))
print("JUNGLE OK (gank)")

# At a gank spot with their wave there and no champion: not a camp (no Smite, no clearing the wave),
# on toward the laner. g32: a 30 s gank spent smiting and clearing the mid wave.
import time as _time
from jev.vision import Unit as _U, View as _V
from jev.minimap import MinimapState as _MS
from jev.riot_api import load_fixture as _lf
tnow = _time.time()
p._obj_pt, p._obj_label = (7000.0, 7000.0), "gank mid"
p.mm_state = _MS(self_pos=(6800.0, 6800.0), ts=tnow)
me_u = _U("champion", "self", 862.0, 490.0, 0.8, (830, 400, 100, 10))
wave = [_U("minion", "enemy", 900.0 + 40 * k, 420.0, 0.3, (0, 0, 60, 4)) for k in range(4)]
p.view = _V(units=wave + [me_u], me=me_u, ts=tnow)
from jev.micro import Micro as _Mi
from jev.control import Controller as _C
from jev import keybinds as _kb
if p.micro is None:
    p.micro = _Mi(_C(dry_run=True, log=lambda m: None), p.screen, _kb.load(), "ORDER")
p.micro.fwd = (1.0, 0.0)
dd = _lf("tests/fixtures/midgame_yasuo_vs_zed.json")
before = p.micro.orders if p.micro else 0
p._do_objective(dd, dd["activePlayer"], dd["activePlayer"]["championStats"], tnow)
act = p.micro.last_action if p.micro else ""
print("at the gank spot with their wave, no champion:", repr(act), "|", p.mech.last_action)
assert "smite" not in act.lower() and "camp" not in act.lower()
print("JUNGLE OK (gank spot is not a camp)")
