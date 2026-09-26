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

# Four of them unseen: the farm walk stops a little past the middle of the lane.
p.mm_state = MinimapState(self_pos=(6500.0, 6500.0), ts=time.time(), enemy_champions=[])  # 4 alive, none seen
p.situation = p._situation(base)
print("unseen:", p.situation.unseen, "| farm cap", p.mech.mia_cap and round(p.mech.mia_cap, 3), "center", round(p.lane.center, 3))
assert p.situation.unseen >= 4 and p.mech.mia_cap is not None and p.mech.mia_cap < p.lane.hard_limit
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

# A lane open to their base in a power play: the nexus tower, then the nexus.
RS = config.RED_STRUCTURES
d3 = with_dead(base, "CHAOS", 3, 30.0)
mmn = MinimapState(self_pos=(11000.0, 11000.0), ts=time.time(), ally_minions=[(12500.0, 12900.0)] * 3)
sN = macro.analyze(d3, mmn, "ORDER", set(), {3, 4, 5})
tN = macro.power_play_target(sN, mmn.pos, mmn, "ORDER", [])
print("mid open:", tN)
assert tN is not None and tN[1] in (config.RED_TOWERS[9], config.RED_TOWERS[10])  # (both next to our minions)
sN = macro.analyze(d3, mmn, "ORDER", set(), {3, 4, 5, 9, 10})
tN = macro.power_play_target(sN, mmn.pos, MinimapState(self_pos=mmn.pos, ally_minions=[(13100.0, 13100.0)] * 3), "ORDER", [])
print("nexus towers down:", tN)
assert tN is not None and tN[1] == RS[-1]
print("MACRO OK (end game)")

# Form: two deaths in the last five minutes play the lane safer; kills a little bolder.
fd = copy.deepcopy(base)
me_name = (fd["activePlayer"].get("riotId") or fd["activePlayer"].get("summonerName"))
fd["gameData"]["gameTime"] = 900.0
fd.setdefault("events", {})["Events"] = [
    {"EventName": "ChampionKill", "EventTime": 700.0, "KillerName": "Zed", "VictimName": me_name, "Assisters": []},
    {"EventName": "ChampionKill", "EventTime": 820.0, "KillerName": "Zed", "VictimName": me_name, "Assisters": []}]
f2 = macro.form(fd)
fd["events"]["Events"] = [{"EventName": "ChampionKill", "EventTime": 850.0, "KillerName": me_name, "VictimName": "Zed", "Assisters": []}]
f3 = macro.form(fd)
print(f"form: two deaths {f2:.2f}, one kill {f3:.2f}")
assert f2 < 0.7 and f3 > 1.0
print("MACRO OK (form)")

# Baron in a late power play: three of them dead for 30+ s, two of us near the pit.
from jev import places as _pl
pit = _pl.places("ORDER")["baron_pit"][0]
d4 = with_dead(base, "CHAOS", 3, 40.0)
d4["gameData"]["gameTime"] = 1500.0
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
p.mm_state = MinimapState(self_pos=(pit[0] - 2500, pit[1] - 2500), ts=time.time(), ally_champions=[(pit[0] + 500, pit[1]), (pit[0], pit[1] - 600)])
p.state = {"me": {"hp_percent": 80, "alive": True, "level": 15}, "objectives": {"next_dragon_in_s": 200, "next_baron_in_s": 0}}
p.situation = macro.analyze(d4, p.mm_state, "ORDER", set(), set())
plan = p._objective_plan(d4, time.time())
print("late power play:", plan)
assert plan is not None and plan[2] == "power play: baron"
print("MACRO OK (baron)")

# A full tick: Jev says recall at 90% HP while two of them are dead for 25 s and our minions are at
# their mid tower: the power play first, the shop after.
from jev.brain import Decision
d5 = with_dead(base, "CHAOS", 2, 25.0)
d5["gameData"]["gameTime"] = 900.0
d5["activePlayer"]["championStats"]["currentHealth"] = 0.9 * d5["activePlayer"]["championStats"]["maxHealth"]
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
p.phase = "lane"
t5 = time.time()
p.mm_state = MinimapState(self_pos=(7600.0, 7400.0), ts=t5, ally_minions=[(mid_outer[0] - 300, mid_outer[1] - 300)] * 3,
                          enemy_champions=[(12000.0, 3000.0)])
p.decision = Decision("recall", 0.8, {"recall": 0.8, "farm": 0.2}, 0.3, 0.9, 0.5, 1.0, None, 0.0, 150, "jev-test", 1400, ts=t5)
p.state = {"me": {"hp_percent": 90, "alive": True, "level": 11}, "objectives": {"next_dragon_in_s": 120}}  # (the brain thread's, live)
p._tick(d5, t5)
print("recall in a power play ->", p.intent, "|", getattr(p, "_obj_label", ""))
assert p.intent == "objective" and "power play" in getattr(p, "_obj_label", "")
print("MACRO OK (no recall in a power play)")

# After laning, nothing on screen for 3 s: walk to the nearest group of their minions on the minimap
# (any lane), not up and down an empty lane (g31: 72% of late farming with no minion on screen).
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
d6 = copy.deepcopy(base)
d6["gameData"]["gameTime"] = 1500.0
p.data = d6
tv = time.time()
me6 = Unit("champion", "self", 862.0, 490.0, 0.9, (830, 400, 100, 10))
p.view = View(units=[me6], me=me6, ts=tv)
p.mm_state = MinimapState(self_pos=(2000.0, 9000.0), ts=tv, enemy_minions=[(6000.0, 6200.0), (6100.0, 6300.0), (6200.0, 6150.0)],
                          enemy_champions=[(12000.0, 12000.0)])
assert not p._farm_the_waves(tv)           # not yet 3 s without a minion
p.view.ts = tv + 3.5                       # (a fresh frame, as live)
assert p._farm_the_waves(tv + 3.5)
print("late farming:", p.mech.last_action)
assert "their wave" in p.mech.last_action
p.mm_state.enemy_champions = [(6300.0, 6400.0), (5900.0, 6000.0)]
p._no_wave_since = tv
p.view.ts = tv + 4.0
assert not p._farm_the_waves(tv + 4.0)     # two of them on that wave: no
print("MACRO OK (farm the waves)")
# The wave in mid while laning top: the lane becomes mid (the micro keeps only its lane's red bars).
p._switch_lane("top")
p._lane_switched_at = tv - 60.0            # (top since a minute ago)
p.mm_state.enemy_champions = []
p._no_wave_since = tv
p.view.ts = tv + 4.0
assert p._farm_the_waves(tv + 4.0)
print("lane after going to the mid wave:", p.lane.name)
assert p.lane.name == "mid"
# ... and not back again on the next tick: g34 switched top/mid three times a second (Jev's "go to
# top lane" against the wave farming). One switch per 10 s.
p.mm_state.enemy_minions = [(1200.0, 11000.0), (1250.0, 11100.0), (1300.0, 11050.0)]   # now a wave in top
p.view.ts = tv + 4.5
p._farm_the_waves(tv + 4.5)
assert p.lane.name == "mid", p.lane.name
p.view.ts = p._lane_switched_at + 10.5
p._farm_the_waves(p._lane_switched_at + 10.5)
print("lane 10 s later, their wave in top:", p.lane.name)
assert p.lane.name == "top"
print("MACRO OK (lane follows the wave)")

# Dragon with allies: two of ours in the pit, dragon up: go. Two of theirs next to me on the way (and
# none of ours), or more of theirs at the pit than of us: not (g33, dead at 29:09 walking through them).
from jev import places as _places
p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
p.state = {"me": {"hp_percent": 90, "alive": True, "level": 11}, "objectives": {"next_dragon_in_s": 0}}
p.situation = macro.analyze(base, None, "ORDER", set(), set())
pit = _places.places("ORDER")["dragon_pit"][0]
ours = [(pit[0] - 300.0, pit[1] + 200.0), (pit[0] + 200.0, pit[1] - 300.0)]
p.mm_state = MinimapState(self_pos=(pit[0] - 3500.0, pit[1] + 2500.0), ts=time.time(), ally_champions=ours, enemy_champions=[])
plan = p._objective_plan(base, time.time())
print("dragon, two of ours there:", plan)
assert plan is not None and plan[2] == "dragon with allies"
me_pt = p.mm_state.self_pos
p.mm_state.enemy_champions = [(me_pt[0] + 500.0, me_pt[1] - 400.0), (me_pt[0] + 700.0, me_pt[1] - 100.0)]
print("... two of theirs next to me:", p._objective_plan(base, time.time()))
assert p._objective_plan(base, time.time()) is None
p.mm_state.enemy_champions = [(pit[0] + 600.0, pit[1] + dy) for dy in (-400.0, 0.0, 400.0, 800.0)]
print("... four of theirs at the pit:", p._objective_plan(base, time.time()))
assert p._objective_plan(base, time.time()) is None
print("MACRO OK (dragon odds)")

# Shopping with the API out of credits and a stale plan whose target is owned already: the next core
# item, not the owned one again (g35 bought Infinity Edge, then "could not buy Infinity Edge" each visit).
from jev.kits import kit_for as _kit_for
p = Player(dry_run=True)
if p.shop_brain is not None:
    p.kit = _kit_for("Yasuo", "TOP")
    p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER", lane=p.lane)
    bought = []
    p.mech.shop = lambda item, items_now: bought.append(item) or True
    p._items_now = lambda: ["Berserker's Greaves", "Infinity Edge", "Doran's Blade"]
    from types import SimpleNamespace as _NS
    p.build = _NS(target="Infinity Edge", ts=time.time() - 600, buy_now=[], needs={})

    def _no_credits(*a, **k):
        raise RuntimeError("402 no credits")

    p.shop_brain.decide = _no_credits
    p.data = {"gameData": {"gameTime": 1400.0}}
    p._shop_plan_and_buy(1300.0)
    print("stale plan, IE owned:", bought, list(p.log_lines)[-2:])
    assert bought and "Infinity Edge" not in bought
    print("MACRO OK (shop without Jev)")
else:
    print("(no item catalog: shop check skipped)")
