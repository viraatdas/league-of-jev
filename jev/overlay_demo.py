"""Replayed fight for `jev overlay-demo`: the overlay's snapshot format, driven by a script of
scenes (Yasuo farming, trading, tornado into ult; Thresh hooking) with a little jitter so the
bars move. Marker positions are around the screen centre, where the locked camera puts the
champion."""
from __future__ import annotations

import random
import time

YASUO_MENU = ["farm", "push", "back_off", "Q", "W", "E", "E_then_Q", "beyblade", "R", "move", "attack_move", "stop",
              "hold", "attack", "flash", "ignite", "potion_slot2", "ward"]
THRESH_MENU = ["hold_with_carry", "push", "back_off", "Q", "flash_hook", "W", "E", "E_pull", "E_push", "R", "move",
               "attack_move", "stop", "hold", "attack", "flash", "ignite", "ward"]
ME, ZED = (862.0, 490.0), (1000.0, 395.0)

SCENES = [
    dict(title="Yasuo laner · mid lane", menu=YASUO_MENU, action="farm", probs={"farm": 0.58, "attack": 0.18, "Q": 0.12, "push": 0.06},
         target=[("minion_12", 0.71, "enemy minion, 9% HP, 175 units NE, killable by one auto"), ("minion_9", 0.18, "enemy minion, 40% HP, 225 units N")],
         where=[("toward_enemy_tower", 0.44), ("NE", 0.31)], distance=0.6, executed="last hit (9%)",
         exec_target=(905.0, 450.0), exec_point=None, intent="farm", champ_hp=74),
    dict(title="Yasuo laner · mid lane", menu=YASUO_MENU, action="E_then_Q", probs={"E_then_Q": 0.46, "Q": 0.21, "attack": 0.12, "farm": 0.09},
         target=[("enemy_champion_101", 0.66, "Zed (enemy champion), 41% HP, 350 units NE"), ("minion_12", 0.21, "enemy minion, 30% HP")],
         where=[("at_target", 0.52), ("NE", 0.30)], distance=1.1, executed="E+Q",
         exec_target=ZED, exec_point=None, intent="trade", champ_hp=41),
    dict(title="Yasuo laner · mid lane", menu=YASUO_MENU, action="Q", probs={"Q": 0.61, "beyblade": 0.14, "E": 0.1, "attack": 0.07},
         target=[("enemy_champion_101", 0.82, "Zed (enemy champion), 33% HP, 400 units NE")],
         where=[("at_target", 0.71), ("NE", 0.2)], distance=1.8, executed="Q3 tornado",
         exec_target=None, exec_point=ZED, intent="all_in", champ_hp=33),
    dict(title="Yasuo laner · mid lane", menu=YASUO_MENU, action="R", probs={"R": 0.62, "beyblade": 0.15, "attack": 0.09, "ignite": 0.06},
         target=[("enemy_champion_101", 0.9, "Zed (enemy champion), 28% HP, 380 units NE, airborne")],
         where=[("at_target", 0.8)], distance=1.9, executed="R Last Breath",
         exec_target=ZED, exec_point=None, intent="all_in", champ_hp=28),
    dict(title="Thresh support · bot lane", menu=THRESH_MENU, action="Q", probs={"Q": 0.52, "flash_hook": 0.14, "hold_with_carry": 0.13, "E_pull": 0.08},
         target=[("enemy_champion_77", 0.74, "Caitlyn (enemy champion), 64% HP, 820 units NE"), ("ally_champion_1", 0.12, "allied champion, 70% HP")],
         where=[("at_target", 0.69), ("NE", 0.2)], distance=2.0, executed="Q hook",
         exec_target=None, exec_point=(1180.0, 330.0), intent="trade", champ_hp=64),
]


class DemoFeed:
    def __init__(self, scene_s: float = 2.6) -> None:
        self.t0 = time.time()
        self.scene_s = scene_s

    def snapshot(self) -> dict:
        now = time.time()
        k = int((now - self.t0) / self.scene_s)
        sc = SCENES[k % len(SCENES)]
        age = (now - self.t0) % self.scene_s
        jit = lambda p: max(0.0, min(1.0, p + random.uniform(-0.02, 0.02)))
        rest = [m for m in sc["menu"] if m not in sc["probs"]]
        menu = [(m, jit(p)) for m, p in sc["probs"].items()] + [(m, jit(0.01)) for m in rest]
        menu.sort(key=lambda kv: -kv[1])
        explored = k % 7 == 6
        mk = {"me": ME, "killable": [(905.0, 450.0)] if sc["action"] == "farm" else [],
              "champ": (*(ZED if "Thresh" not in sc["title"] else (1180.0, 330.0)), sc["champ_hp"],
                        "Zed" if "Thresh" not in sc["title"] else "Caitlyn"),
              "exec": {"name": sc["executed"], "target": sc["exec_target"], "point": sc["exec_point"], "age": max(0.0, age - 0.3)}}
        return {
            "header": {"title": sc["title"], "stats": "7:55  L9  HP 62%  gold 1320  cs 71  2/1/1", "input": "demo"},
            "tactics": {"action": sc["action"], "p": jit(sc["probs"][sc["action"]]), "explored": explored, "fits": jit(0.72),
                        "latency": random.uniform(140, 230), "rate": 6.4, "menu": menu, "target": sc["target"], "where": sc["where"],
                        "distance": sc["distance"], "executed": sc["executed"] if age > 0.3 else None, "exec_age": max(0.0, age - 0.3)},
            "strategy": {"intent": sc["intent"], "jev_intent": sc["intent"], "p": jit(0.48), "latency": 210,
                         "probs": [(sc["intent"], jit(0.48)), ("farm" if sc["intent"] != "farm" else "trade", jit(0.22)), ("go_to", jit(0.12)), ("recall", jit(0.08))],
                         "danger": 1.3 + random.uniform(-0.1, 0.1), "aggr": 1.2, "recall": jit(0.34), "fight": jit(0.66),
                         "destination": [("dragon_pit", 0.54), ("bot_lane", 0.21), ("my_mid_tower", 0.09)], "level_up": "Q" if k % 5 == 0 else None},
            "build": {"target": "Mercurial Scimitar", "p": 0.43, "buy_now": ["Quicksilver Sash", "Long Sword"],
                      "needs": {"armor": 0.2, "mr": 0.89, "tenacity": 0.87, "antiheal": 0.09, "defense": 0.78}},
            "perf": {"apm": random.randint(260, 340), "capture": "sck", "fps": 60, "read_ms": 7.1, "api_ms": 11,
                     "react": {"reflex": (15, 22), "lasthit": (16, 24), "jev": (190, 262)}},
            "micro": {"mode": sc["intent"] if sc["intent"] in ("trade", "all_in") else "farm",
                      "order": (f"{sc['intent']}: {sc['executed']}" if sc["intent"] in ("trade", "all_in") else "farm: hold behind the wave"),
                      "age": max(0.0, age - 0.3)},
            "log": ["build -> Mercurial Scimitar", "level Q (Jev)", f"did: {sc['executed']}"],
            "markers": mk,
        }
