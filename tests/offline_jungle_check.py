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

# A camp is "killed" only when its large monster was last seen low: g36's blue buff healed back to 69%
# off screen after Lee walked off it (a small wolf had read 5%), was marked killed, and Lee was level 2
# at 5:15. Last seen at 69% and gone: back to it. Last seen at 12% and gone: cleared.
from types import SimpleNamespace as NS
for big_hp, want_cleared in ((0.69, False), (0.12, True)):
    p = Player(dry_run=True, champion="leesin", role="JUNGLE")
    p.kit = LeeSin("JUNGLE")
    p.side = "ORDER"
    p.lane = Lane("mid", "ORDER")
    p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane, skill_order=p.kit.skill_order)
    p.jungle_state = JungleState("ORDER")
    p._micro_step = lambda *a, **k: False
    p._recent_gold_jump = lambda s: 0.0
    blue = camps("ORDER")["blue"]
    p.jungle_state.current, p.jungle_state.arrived_at = "blue", 200.0
    p.mm_state = MinimapState(self_pos=blue, ts=time.time())
    d2 = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
    for i in range(0, 16):
        gt = 200.0 + i
        d2["gameData"]["gameTime"] = gt
        p.mm_state.ts = time.time()
        units = [NS(unit=NS(kind="monster", hp=big_hp)), NS(unit=NS(kind="minion", hp=0.05))] if i < 10 else []
        p.scene = NS(minions=units)
        p._jungle_step(d2, d2["activePlayer"], d2["activePlayer"].get("championStats", {}), time.time())
    print(f"blue last seen at {big_hp:.0%}, gone 6 s:", "cleared" if "blue" in p.jungle_state.cleared else "not cleared",
          "| current:", p.jungle_state.current, p.jungle_state.arrived_at)
    assert ("blue" in p.jungle_state.cleared) == want_cleared
print("JUNGLE OK (large monster decides)")

# On a camp, autos and Q stay on the camp in front of me: a low bar 700 units off is not the target.
from jev import config as _c, keybinds
from jev.control import Controller
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.vision import Unit
PPU = _c.VISION.px_per_unit
ME = (862.0, 490.0)
def trk(dx, hp, kind, tid):
    u = Unit(kind, "enemy", ME[0] + dx * PPU, ME[1], hp, (0, 0, 60, 4))
    t = Track(id=tid, unit=u, seen=time.time())
    t.hist.append((time.time(), hp))
    return t
lee = LeeSin("JUNGLE")
mi = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
buff, far = trk(250, 0.8, "monster", 1), trk(700, 0.2, "minion", 2)
sc = Scene(me_xy=ME, minions=[buff, far])
sc.ready = {}
lee.continuous(mi, sc, time.time(), 0.7, "farm", False)
print("camp target:", mi.last_action)
assert "monster 80%" in mi.last_action
print("JUNGLE OK (camp target)")

# A gank that finds its target goes in, whatever the fight head's timid read (32 ganks over g30, g32
# and g36 made no kill). Not when Jev reads me about to die.
from jev.fight import FightRead
from jev.minimap import dist as _dist
for danger, want in ((0.2, "all_in"), (0.9, None)):
    p = Player(dry_run=True, champion="leesin", role="JUNGLE")
    p.kit = LeeSin("JUNGLE")
    p.jungle_state = JungleState("ORDER")
    p.micro = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
    p.micro.hp_pct = 85.0
    p.intent = "objective"
    p._gank = {"lane": "top", "pt": (3000.0, 12000.0), "seen": time.time(), "until": time.time() + 30}
    her = trk(900, 0.9, "champion", 5)
    p.champ_tracker.tracks = {5: her}
    sc = Scene(me_xy=ME)
    sc.champ, sc.champ_dist, sc.enemy_champs = her, sc.dist(her), 1
    sc.ready = {"Q": True}
    fr = FightRead(seq=1, ts=time.time(), latency_ms=150.0, plan="back_off", plan_probs={"back_off": 0.6}, win_all_in=0.2,
                   trade_worth=0.4, in_danger=danger, gank_coming=0.4)
    p.fights = NS(read=fr, log=NS(record=lambda *a, **k: None))
    p._fight_triggers(sc, p.micro, time.time())
    print(f"gank, target at 900u, Jev back_off with danger {danger}:", p.micro.mode, "|", list(p.log_lines)[-1:])
    assert (p.micro.mode == "all_in") == (want == "all_in")
# Far from the target: walk to 500 units past her toward their tower (where she will run), not to her.
p = Player(dry_run=True, champion="leesin", role="JUNGLE")
p.side = "ORDER"
g = {"lane": "top", "pt": Lane("top", "ORDER").point(0.42)}
mm = MinimapState(self_pos=(4000.0, 7000.0), ts=time.time())
ap = p._gank_approach(g, mm)
ln = Lane("top", "ORDER")
print("approach:", tuple(int(v) for v in ap), "her at", tuple(int(v) for v in g["pt"]), "progress", round(ln.project(ap)[0], 3), "vs", round(ln.project(g["pt"])[0], 3))
assert ln.project(ap)[0] > ln.project(g["pt"])[0] + ln.frac(400)
mm.self_pos = (g["pt"][0] + 800.0, g["pt"][1])
assert p._gank_approach(g, mm) == g["pt"]
print("JUNGLE OK (gank commit and approach)")
