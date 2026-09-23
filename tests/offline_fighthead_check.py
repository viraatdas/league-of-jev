"""Fight head: one real Jev call on two hand-made fight states (a winnable duel, and a gank at half
HP), then the plan -> mode mapping and its floors on a dry-run player (no game).
Run: uv run python tests/offline_fighthead_check.py"""
import time

from jev import config, keybinds
from jev.control import Controller
from jev.fight import FightBrain, FightRead
from jev.loop import Player
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.vision import Unit

brain = FightBrain("a Yasuo laner in the mid lane")
duel = {
    "me": {"champion": "Yasuo", "level": 6, "hp_percent": 85, "hp": "900/1060", "hp_last_1s": 0, "hp_last_3s": -5,
           "abilities": {"Q": "Q3 tornado ready", "W": "ready", "E": "ready", "R": "not castable"},
           "summoner_spells": {"flash": "ready", "ignite": "ready"}, "attack_damage": 110, "armor": 45, "magic_resist": 38,
           "items": ["Doran's Blade", "Pickaxe"], "enemy_minions_around_me": 1, "ally_minions_on_screen": 5, "allied_champions_on_screen": 0},
    "enemies_on_screen": {"enemy_champion_7": {"hp_percent": 30, "hp_last_1s": -12, "hp_last_3s": -35, "distance": 450, "direction": "NE",
                                                "moving": "away from me", "enemy_minions_around_them": 0,
                                                "describe": "enemy champion at 30% HP, 450 units NE, away from me"}},
    "where": {"inside_enemy_tower_range": False, "distance_to_my_nearest_tower": 2400, "distance_to_their_nearest_tower": 2100},
    "map": {"enemy_champions_within_4000": [{"distance": 500, "direction": "NE"}], "allied_champions_within_4000": [],
            "enemy_champions_not_on_the_minimap": 1},
    "lane_opponent": {"champion": "Kayle", "level": 5, "items": ["Doran's Ring"], "kda": "0/1/0"},
    "enemy_team": [{"champion": "Kayle", "class": "fighter", "damage": "mixed", "level": 5, "kda": "0/1/0"}],
    "game_time": "7:10",
    "numbers": {"enemy_champions_within_1000": 1, "our_champions_within_1000_including_me": 1, "seconds_i_last_at_this_rate": None,
                "their_hp_total_percent_on_screen": 30},
}
gank = dict(duel, me=dict(duel["me"], hp_percent=48, hp="510/1060", hp_last_1s=-14, hp_last_3s=-30,
                          abilities={"Q": "on cooldown", "W": "not ready", "E": "ready", "R": "not castable"},
                          summoner_spells={"flash": "ready", "ignite": "not ready"}),
            enemies_on_screen={
                "enemy_champion_7": dict(duel["enemies_on_screen"]["enemy_champion_7"], hp_percent=92, hp_last_1s=0, hp_last_3s=0,
                                         moving="toward me", describe="enemy champion at 92% HP, 350 units NE, toward me"),
                "enemy_champion_9": {"hp_percent": 100, "hp_last_1s": 0, "hp_last_3s": 0, "distance": 600, "direction": "N",
                                     "moving": "toward me", "enemy_minions_around_them": 0,
                                     "describe": "enemy champion at 100% HP, 600 units N, toward me"}},
            map={"enemy_champions_within_4000": [{"distance": 400, "direction": "NE"}, {"distance": 600, "direction": "N"}],
                 "allied_champions_within_4000": [], "enemy_champions_not_on_the_minimap": 0},
            numbers={"enemy_champions_within_1000": 2, "our_champions_within_1000_including_me": 1, "seconds_i_last_at_this_rate": 3.4,
                     "their_hp_total_percent_on_screen": 192})
reads = {}
for name, st in (("duel", duel), ("gank", gank)):
    r = brain.ask(st, time.time())
    reads[name] = r
    print(f"{name}: {r.summary()}  ({r.latency_ms:.0f} ms)")
assert reads["gank"].in_danger >= 0.5 and reads["gank"].in_danger > reads["duel"].in_danger, "a two-on-one at half HP is dangerous"
assert reads["gank"].plan in ("back_off", "escape"), reads["gank"].plan

# plan -> mode, with the floors
p = Player(dry_run=True)
p.micro = Micro(Controller(dry_run=True, log=lambda m: None), Screen(), keybinds.load(), "ORDER")
p.fights = brain
now = time.time()
ME = (862.0, 490.0)


def track(dx, hp, tid):
    u = Unit("champion", "enemy", ME[0] + dx * config.VISION.px_per_unit, ME[1], hp, (0, 0, 10, 10))
    t = Track(id=tid, unit=u, seen=now)
    return t


def read(plan, win=0.5, trade=0.5, danger=0.1):
    return FightRead(seq=int(now * 1000) % 100000 + hash(plan) % 97, ts=now, latency_ms=150, plan=plan, plan_probs={plan: 0.6},
                     win_all_in=win, trade_worth=trade, in_danger=danger, gank_coming=0.1)


sc = Scene(me_xy=ME)
sc.champ = track(400, 0.25, 7)
sc.champ_dist, sc.enemy_champs = sc.dist(sc.champ), 1
p._apply_fight_read(read("all_in", win=0.8), sc, p.micro, now)
assert p.micro.mode == "all_in" and p.micro.flash_in_ok, (p.micro.mode, p.micro.flash_in_ok)
p._apply_fight_read(read("all_in", win=0.4, trade=0.6), sc, p.micro, now + 3)
assert p.micro.mode == "trade", p.micro.mode  # Jev not sure of the kill: a trade instead
sc.enemy_champs = 2
p._apply_fight_read(read("trade", trade=0.7), sc, p.micro, now + 6)
assert p.micro.mode == "back_off", p.micro.mode  # two on one with no ally: floor
sc.enemy_champs = 1
p._apply_fight_read(read("farm", danger=0.9), sc, p.micro, now + 9)
assert p.micro.mode == "back_off" and p._force_escape_until > now + 9, p.micro.mode  # about to die: escape
# the live feature builder on fixture data (runs in game on every published scene)
import json as _json
from jev.riot_api import load_fixture
data = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
p.micro.summoners, p.micro.hp_pct = ["flash", "ignite"], 60.0
for k in range(12):
    p._self_trail.append((now - 1.2 + k * 0.1, 80 - k * 2))
    sc.champ.trail.append((now - 1.2 + k * 0.1, 0.6 - k * 0.02))
feats = p._fight_features(sc, [sc.champ], now, data)
print(_json.dumps(feats)[:400])
assert feats["numbers"]["seconds_i_last_at_this_rate"] is not None and feats["me"]["hp_last_1s"] < 0
assert feats["enemies_on_screen"] and list(feats["enemies_on_screen"].values())[0]["hp_last_1s"] < 0
print("FIGHT HEAD OK")
