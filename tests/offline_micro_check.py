"""Offline check of the fast path on a saved lane screenshot, for both kits: vision -> tracks ->
scene -> one real tactical Jev call -> kit executor (dry run, logs the orders it would send)."""
import sys
import time

import cv2

from jev import keybinds
from jev.control import Controller
from jev.kits import Thresh, Yasuo
from jev.micro import Micro, UnitTracker, build_scene
from jev.screen import Screen
from jev.tactics import TacticalBrain

shot = sys.argv[1] if len(sys.argv) > 1 else "snapshots/054108.png"
img = cv2.imread(shot)
assert img is not None, f"missing {shot}"
from jev.vision import VisionReader

vr = VisionReader()
tr_m, tr_c = UnitTracker(), UnitTracker(max_jump_px=90)
now = time.time()
for i in range(4):  # a few frames so HP trends exist
    view = vr.read(img)
    mins = tr_m.update(view.enemies("minion"), now + i * 0.05)
    champs = tr_c.update(view.enemies("champion"), now + i * 0.05)
sc = build_scene(view, mins, champs, ad=68.0, q_rank=1, game_s=180.0, now=now + 0.2, fallback_xy=(862, 490))
print(f"vision {view.ms:.1f} ms  me={view.me and (int(view.me.x), int(view.me.y))}  minions={len(sc.minions)} killable={len(sc.killable_auto)} ready={sc.ready}")
api = {"me": {"level": 2, "hp_percent": 81}, "lane_opponent": {"champion": "Zed", "level": 2}}

for kit in (Yasuo(), Thresh()):
    log = []
    ctl = Controller(dry_run=True, log=log.append)
    mi = Micro(ctl, Screen(), keybinds.load(), "ORDER")
    acts = kit.available(sc, mi, time.time())
    plan = {"intent": "farm", "lane": "mid" if kit.name == "Yasuo" else "bot", "aggression_0_to_2": 1.0, "danger": 0.4, "fight_favorable": 0.5}
    t = TacticalBrain().ask(sc, kit, mi, api, plan, time.time())
    assert t is not None
    print(f"\n{kit.name} ({kit.role}) menu={acts}")
    print(f"  jev: {t.action} p={t.confidence:.2f} fits={t.menu_fits:.2f} {t.latency_ms:.0f} ms {t.tokens} tok  {t.probabilities}")
    if t.action in ("farm", "push", "back_off", "hold_with_carry"):
        mi.mode = t.action
        ok = kit.continuous(mi, sc, time.time(), 0.7, mi.mode, False)
    else:
        ok = kit.execute(mi, t.action, sc, time.time(), 0.7, t.extra)
    print(f"  executed={ok} action='{mi.last_action}' orders={log}")
    if kit.name == "Yasuo":
        assert "q_minions" in acts and "farm" in acts
    else:
        assert "hold_with_carry" in acts and "farm" not in acts
print("MICRO OK")
