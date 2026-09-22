"""Offline check of the fast path on a saved lane screenshot: vision -> tracks -> scene ->
one real tactical Jev call -> micro executor (dry run, logs the orders it would send)."""
import sys
import time
from pathlib import Path

import cv2

from jev import keybinds
from jev.control import Controller
from jev.micro import Micro, UnitTracker, build_scene
from jev.screen import Screen
from jev.tactics import TacticalBrain, available, tactical_state
from jev.vision import VisionReader

shot = sys.argv[1] if len(sys.argv) > 1 else "snapshots/054108.png"
img = cv2.imread(shot)
assert img is not None, f"missing {shot}"
vr = VisionReader()
tr_m, tr_c = UnitTracker(), UnitTracker(max_jump_px=90)
now = time.time()
for i in range(4):  # a few frames so HP trends exist
    view = vr.read(img)
    mins = tr_m.update(view.enemies("minion"), now + i * 0.05)
    champs = tr_c.update(view.enemies("champion"), now + i * 0.05)
sc = build_scene(view, mins, champs, ad=68.0, q_rank=1, game_s=180.0, now=now + 0.2, fallback_xy=(862, 490))
print(f"vision {view.ms:.1f} ms  me={view.me and (int(view.me.x), int(view.me.y))}  minions={len(sc.minions)} killable={len(sc.killable_auto)} q_killable={len(sc.killable_q)} ready={sc.ready}")
acts = available(sc, q3=False)
print("available:", acts)
assert "farm" in acts and "q_minions" in acts

api = {"me": {"level": 2, "hp_percent": 81}, "lane_opponent": {"champion": "Zed", "level": 2}}
plan = {"intent": "farm", "aggression_0_to_2": 1.0, "danger": 0.4, "fight_favorable": 0.5}
print("state:", tactical_state(sc, False, api, plan))
tb = TacticalBrain()
tb.publish(sc, False, api, plan, time.time())
import threading
th = threading.Thread(target=tb.run, daemon=True)
th.start()
t0 = time.time()
while tb.tactic is None and time.time() - t0 < 5:
    time.sleep(0.02)
tb.stop()
t = tb.tactic
assert t is not None, tb.last_error
print(f"jev tactic: {t.action} p={t.confidence:.2f} {t.latency_ms:.0f} ms, {t.tokens} tokens  probs={t.probabilities}")

log = []
ctl = Controller(dry_run=True, log=log.append)
mi = Micro(ctl, Screen(), keybinds.load(), "ORDER")
ok = mi.execute(t.action, sc, time.time(), 0.7) if t.action not in ("farm", "push", "back_off") else mi.farm_step(sc, time.time(), 0.7, push=t.action == "push")
print("executed:", ok, mi.last_action, log)
ok2 = mi.farm_step(sc, time.time() + 2, 0.7)
print("farm step:", ok2, mi.last_action, log[-3:])
print("MICRO OK")
