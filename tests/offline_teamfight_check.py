"""Fight entries without Jev (credits out, stale reads): join a team fight when allied champions
are on screen and the numbers are even, all in on a lane opponent who is low while I am healthy,
and focus the weakest champion in reach rather than the nearest. Dry-run player, no game.
Run: uv run python tests/offline_teamfight_check.py"""
import time

from jev import config as _config
_config.FAST.lane_planner = False  # these check the rule path; tests/offline_laneplan_check.py checks the planner

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
# Team fight: one ally on screen next to her, one enemy at 70%: all in.
p = setup(now)
e = track(500, 0.7, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME, ally_champs=[Unit("champion", "ally", e.unit.x - 100 * PPU, ME[1], 0.8, (0, 0, 1, 1))])
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

# Lee Sin gank plan: their top laner pushed past the middle toward us, our top laner there, me level 4.
from jev.minimap import MinimapState
p = setup(now)
p.jungle_state = object()
p.side = "ORDER"
from jev.lanes import Lane
top = Lane("top", "ORDER")
enemy_at = top.point(top.center - top.frac(900))
ally_at = top.point(top.center - top.frac(1400))
mm = MinimapState()
mm.self_pos, mm.ts = (2288.0, 8448.0), now  # at our gromp
mm.enemy_champions, mm.ally_champions = [enemy_at, (12000.0, 12000.0)], [ally_at]
g = p._gank_plan(mm, mm.ally_champions, 300.0, {"level": 4, "hp_percent": 90}, now)
print("gank:", g, list(p.log_lines)[-1:])
assert g is not None and g[2] == "gank top"
# Two of them there and none of us, Lee still on the way: over once it holds 3 s.
mm.enemy_champions = [enemy_at, (enemy_at[0] + 300, enemy_at[1])]
g = p._gank_plan(mm, [], 300.0, {"level": 4, "hp_percent": 90}, now + 1)
assert g is not None  # one frame of bad numbers is not enough (the minimap jitters)
g = p._gank_plan(mm, [], 300.0, {"level": 4, "hp_percent": 90}, now + 2.6)
assert g is not None  # held 1.6 s: still on (3 s now: icons merge in skirmishes, g21)
g = p._gank_plan(mm, [], 300.0, {"level": 4, "hp_percent": 90}, now + 4.2)
assert g is None and "over" in list(p.log_lines)[-1], list(p.log_lines)[-1]
# Level 2: no gank.
p._gank_next = 0
mm.enemy_champions = [enemy_at]
assert p._gank_plan(mm, [ally_at], 300.0, {"level": 2, "hp_percent": 90}, now) is None
print("GANK OK")

# Under our tower: a healthy enemy chasing me next to our mid outer tower is a fight.
from jev import config as _cfg
p = setup(now)
p.side = "ORDER"
mm = MinimapState()
tw = _cfg.BLUE_TOWERS[3]  # mid outer
mm.self_pos, mm.ts = (tw[0] - 200, tw[1] - 200), now
p.mm_state = mm
p.micro.hp_pct = 55.0
e = track(300, 0.85, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
p._fight_triggers(sc, p.micro, now)
print("tower:", p.micro.mode, list(p.log_lines)[-1:])
assert p.micro.mode == "all_in" and "under our tower" in list(p.log_lines)[-1]
p._dead_turrets = {"Turret_T1_C_05_A"}
p.micro.set_mode("farm", now)
p._fight_triggers(sc, p.micro, now + 5)
assert p.micro.mode != "all_in" or "under our tower" not in list(p.log_lines)[-1]
print("TOWER OK")

# Execute: a champion steady at 8% within Q range gets the Q, even while I am at 20% and backing off.
p = setup(now)
p.micro.hp_pct = 20.0
p.micro.set_mode("back_off", now)
low = track(400, 0.08, 9, now)
p.champ_tracker.tracks = {9: low}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = low, sc.dist(low), 1
sc.ready = {"Q": True, "E": True}
assert p._execute(p.kit, p.micro, sc, now), list(p.log_lines)[-1:]
print("execute:", list(p.log_lines)[-1])
assert "Q the low champion" in p.micro.last_action
# A flickering bar (one reading at 60%) is not a kill.
flick = track(400, 0.08, 10, now)
flick.hist[2] = (flick.hist[2][0], 0.6)
p.champ_tracker.tracks = {10: flick}
p.micro.last_order = 0
assert not p._execute(p.kit, p.micro, sc, now + 0.01)
print("EXECUTE OK")

# Join a fight: two of ours on one of theirs 2500 units away.
p = setup(now)
mm = MinimapState()
mm.self_pos, mm.ts = (6000.0, 6000.0), now
e = (7600.0, 7800.0)
mm.enemy_champions, mm.ally_champions = [e], [(7800.0, 7700.0), (7500.0, 8000.0)]
j = p._join_fight_plan(mm, mm.ally_champions, 600.0, 80.0, now)
print("join:", j, list(p.log_lines)[-1])
assert j is not None and j[2] == "join the fight"
# Three of theirs on one of ours: stay out.
p = setup(now)
mm.enemy_champions, mm.ally_champions = [e, (8200.0, 7600.0), (7900.0, 7300.0)], [(8300.0, 7400.0)]
assert p._join_fight_plan(mm, mm.ally_champions, 600.0, 80.0, now) is None
print("JOIN OK")

# A trade that left her at 30% while I am at 85%: upgrade to all in.
p = setup(now)
p.micro.hp_pct = 85.0
p.micro.set_mode("trade", now)
e = track(500, 0.3, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
p._fight_triggers(sc, p.micro, now)
print("trade won:", p.micro.mode, list(p.log_lines)[-1])
assert p.micro.mode == "all_in"
print("TRADE UPGRADE OK")

# Q and E down, a healthier champion at 300 units, no ally: retreat.
p = setup(now)
p.micro.hp_pct = 65.0
e = track(300, 0.99, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
sc.ready = {"W": True}
p._fight_triggers(sc, p.micro, now)
print("nothing up:", p.micro.mode, list(p.log_lines)[-1])
assert p.micro.mode == "back_off" and p.guards.retreat_until > now
print("NOTHING UP OK")

# Jev hysteresis: a low-confidence plan change needs a second read; a confident one takes over at once.
from types import SimpleNamespace
from jev.fight import FightLog, FightRead
p = setup(now)
p.fights = SimpleNamespace(log=FightLog(None))
e = track(500, 0.8, 1, now)
p.champ_tracker.tracks = {1: e}
sc = Scene(me_xy=ME)
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
def fr(plan, prob, seq):
    return FightRead(seq=seq, ts=now, latency_ms=100, plan=plan, plan_probs={plan: prob}, win_all_in=0.3, trade_worth=0.6,
                     in_danger=0.1, gank_coming=0.1, state={"enemies_on_screen": {"enemy_champion_1": {"hp_percent": 80}}})
p._apply_fight_read(fr("farm", 0.6, 1), sc, p.micro, now)
p._apply_fight_read(fr("back_off", 0.35, 2), sc, p.micro, now + 0.5)
assert p._fight_plan_held == "farm", p._fight_plan_held      # one unsure read does not flip
p._apply_fight_read(fr("back_off", 0.35, 3), sc, p.micro, now + 1.0)
assert p._fight_plan_held == "back_off", p._fight_plan_held  # the second one does
p._apply_fight_read(fr("trade", 0.55, 4), sc, p.micro, now + 1.5)
assert p._fight_plan_held == "trade", p._fight_plan_held     # a sure one at once
print("HYSTERESIS OK")

# Jev wants a trade but its trade_worth is under the floor (poke): the kit's window opens it when EQ
# is up and she is in E reach with no wave around her. Out of E reach, no trade (no walk after her).
from jev.kits import Yasuo
for dist_u, want in [(420, "trade"), (600, "poke")]:
    p = setup(now)
    p.kit = Yasuo()
    p.fights = SimpleNamespace(log=FightLog(None))
    p.micro.hp_pct = 90.0
    e = track(dist_u, 0.8, 1, now)
    p.champ_tracker.tracks = {1: e}
    sc = Scene(me_xy=ME, ready={"Q": True, "E": True})
    sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
    read = fr("trade", 0.45, 1)
    read.trade_worth = 0.47
    p._apply_fight_read(read, sc, p.micro, now)
    print(f"jev trade at {dist_u}u, worth 0.47:", p.micro.mode, list(p.log_lines)[-1:])
    assert p.micro.mode == want, p.micro.mode

# Poke: she walks into auto range and no minion is up for a last hit: auto her. A killable minion first.
p = setup(now)
p.kit = Yasuo()
mi = p.micro
mi.set_mode("poke", now)
e = track(200, 0.8, 1, now)
sc = Scene(me_xy=ME, ready={})
sc.champ, sc.champ_dist, sc.enemy_champs = e, sc.dist(e), 1
mi.last_order = 0
p.kit.poke(mi, sc, now, 0.7)
print("poke, she is in reach:", mi.last_action)
assert mi.last_action == "poke: auto the champion"
m = track(150, 0.1, 7, now)
m.unit.kind = "minion"
sc.minions, sc.killable_auto = [m], [m]
mi.last_order, mi.last_attack = 0, 0
p.kit.poke(mi, sc, now + 2, 0.7)
print("poke, a minion to last hit:", mi.last_action)
assert mi.last_action != "poke: auto the champion"
print("TRADE WINDOW OK")

# Sidestep only against line skillshots: Nasus (melee) no, Annie (ranged) yes.
p = setup(now)
if p.shop_brain is not None:
    p.state = {"lane_opponent": {"champion": "Nasus"}}
    assert p._opponent_throws_lines() is False
    p.state = {"lane_opponent": {"champion": "Annie"}}
    assert p._opponent_throws_lines() is True
mi = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
mi.fwd, mi.dodge_lines = (1.0, 0.0), False
m = track(300, 0.9, 5, now)
m.unit.kind = "minion"
sc = Scene(me_xy=ME, minions=[m])
c = track(900, 0.9, 6, now)
sc.champ, sc.champ_dist = c, sc.dist(c)
mi.last_order = 0
mi.farm_step(sc, now, 0.7)
print("melee opponent in range:", mi.last_action)
assert not mi.last_action.startswith("farm: sidestep")
print("SIDESTEP OK")
