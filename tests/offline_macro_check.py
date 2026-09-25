"""The macro situation (macro.py) and what the loop does with it: a power play (two of them dead
for a while) takes their frontmost tower where our minions are, not a tower with nobody of ours; two
of us dead means no skirmishes; three of them unseen stops the farm walk at the middle of the lane.
Run: uv run python tests/offline_macro_check.py"""
import copy
import time

from jev import config, macro
from jev.loop import Player
from jev.mechanics import Mechanics
from jev.minimap import MinimapState
from jev.riot_api import load_fixture

base = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
base["gameData"]["gameTime"] = 900.0
me_team = next(p["team"] for p in base["allPlayers"] if p.get("position") == "MIDDLE" and p["team"] == "ORDER")


def with_dead(data, team, n, respawn):
    d = copy.deepcopy(data)
    k = 0
    for p in d["allPlayers"]:
        if p["team"] == team and k < n and p.get("position") != "MIDDLE":
            p["isDead"], p["respawnTimer"] = True, respawn
            k += 1
    return d


RT = config.RED_TOWERS
mid_outer = RT[3]
mm = MinimapState(self_pos=(6500.0, 6500.0), ts=time.time(),
                  ally_minions=[(mid_outer[0] - 300, mid_outer[1] - 300)] * 3, enemy_champions=[(12000.0, 3000.0)])

# Two of them dead for 20+ s: a power play; the target is their mid outer, where our minions are.
d = with_dead(base, "CHAOS", 2, 22.0)
s = macro.analyze(d, mm, "ORDER", set(), set())
print("situation:", s.summary()["window"], "| alive", s.allies_alive, s.enemies_alive)
assert s.power_play and not s.outnumbered
t = macro.power_play_target(s, mm.pos, mm, "ORDER", [])
print("power play target:", t)
assert t is not None and t[1] == mid_outer
# Their mid outer already down: the next one in (mid inner) is the target only with our minions there.
s2 = macro.analyze(d, mm, "ORDER", set(), {3})
t2 = macro.power_play_target(s2, mm.pos, mm, "ORDER", [])
print("mid outer down, no minions at the inner:", t2)
assert t2 is None

# Short respawns are no window.
s3 = macro.analyze(with_dead(base, "CHAOS", 2, 5.0), mm, "ORDER", set(), set())
assert not s3.power_play

# The loop: the objective plan follows the power play; two of us dead blocks skirmishes.
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
p.mm_state = mm
p.state = {"me": {"hp_percent": 90, "alive": True, "level": 11}, "objectives": {"next_dragon_in_s": 60}}
p.situation = s
plan = p._objective_plan(d, time.time())
print("objective plan:", plan)
assert plan is not None and "power play" in plan[2]
p.situation = macro.analyze(with_dead(base, "ORDER", 3, 30.0), mm, "ORDER", set(), set())  # (the fixture has Graves dead)
assert p.situation.outnumbered
mm2 = MinimapState(self_pos=(6500.0, 6500.0), ts=time.time(), ally_champions=[(8000.0, 8200.0)],
                   enemy_champions=[(8200.0, 8300.0)])
p.mm_state = mm2
assert p._join_fight_plan(mm2, mm2.ally_champions, 900.0, 90.0, time.time()) is None
print("outnumbered: no skirmish")

# Three of them unseen: the farm walk stops at the middle of the lane.
p.mm_state = MinimapState(self_pos=(6500.0, 6500.0), ts=time.time(), enemy_champions=[(12000.0, 3000.0)])  # 4 alive, 1 seen
p.situation = p._situation(base)
print("unseen:", p.situation.unseen, "| farm cap", p.mech.mia_cap and round(p.mech.mia_cap, 3), "center", round(p.lane.center, 3))
assert p.situation.unseen >= 3 and p.mech.mia_cap is not None and p.mech.mia_cap < p.lane.hard_limit
print("MACRO OK")

# Shove before a recall: the wave on screen gets pushed first (up to 10 s) when it is safe to.
from jev.vision import Unit, View
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
p.situation = macro.analyze(base, None, "ORDER", set(), set())
p.situation.unseen = 0
me = Unit("champion", "self", 862.0, 490.0, 0.9, (830, 400, 100, 10))
wave = [Unit("minion", "enemy", 1000.0 + 30 * k, 400.0, 0.8, (0, 0, 60, 4)) for k in range(3)]
ours = [Unit("minion", "ally", 950.0, 450.0, 0.9, (0, 0, 60, 4))]
p.view = View(units=wave + ours + [me], me=me, ts=time.time())
p._micro_step = lambda *a, **k: True          # the push itself is the lane layer's (tested elsewhere)
t0 = time.time()
assert p._shove_first({}, {}, {}, t0, 80.0)
print("shove first:", p.mech.last_action)
assert not p._shove_first({}, {}, {}, t0 + 11.0, 80.0)          # 10 s at most, then home
p._shove_t0 = -1e9
assert not p._shove_first({}, {}, {}, t0 + 60.0, 40.0)          # hurt: straight home
her = Unit("champion", "enemy", 1100.0, 380.0, 0.9, (0, 0, 100, 10))
p.view = View(units=wave + ours + [me, her], me=me, ts=time.time())
p._shove_t0 = -1e9
assert not p._shove_first({}, {}, {}, t0 + 120.0, 80.0)         # their champion there: no
print("MACRO OK (shove)")
