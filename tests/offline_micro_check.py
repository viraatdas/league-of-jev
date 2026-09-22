"""Offline check of the full action space on a saved lane screenshot, for both kits: vision ->
tracks -> scene -> menu (kit + universal + summoners + items) -> one real Jev call with the
action / target / where / distance heads -> executor on a dry-run controller."""
import sys
import time

import cv2

from jev import actions, keybinds
from jev.control import Controller
from jev.kits import Thresh, Yasuo
from jev.lanes import Lane
from jev.micro import Micro, UnitTracker, build_scene
from jev.screen import Screen
from jev.tactics import TacticalBrain, TacticInput, menu
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
print(f"vision {view.ms:.1f} ms  minions={len(sc.minions)} killable={len(sc.killable_auto)} ready={sc.ready} items_ready={view.hud.items_ready}")
api = {"me": {"level": 2, "hp_percent": 70}, "lane_opponent": {"champion": "Zed", "level": 2}}
items = [{"displayName": "Doran's Blade", "slot": 0, "canUse": False, "consumable": False},
         {"displayName": "Health Potion", "slot": 1, "canUse": True, "consumable": True},
         {"displayName": "Stealth Ward", "slot": 6, "canUse": True, "consumable": False}]

for kit, lane in ((Yasuo(), Lane("mid", "ORDER")), (Thresh(), Lane("bot", "ORDER"))):
    log = []
    ctl = Controller(dry_run=True, log=log.append)
    mi = Micro(ctl, Screen(), keybinds.load(), "ORDER")
    ctx = actions.Ctx(mi=mi, sc=sc, kit=kit, lane=lane, now=time.time(), aspd=0.7, ally_units=view.allies("champion"), lane_progress=0.48)
    plan = {"intent": "farm", "lane": lane.name, "aggression_0_to_2": 1.0, "danger": 0.4, "fight_favorable": 0.5}
    inp = TacticInput(ctx=ctx, api=api, plan=plan, ts=time.time(), summoners=["flash", "ignite"], items=items,
                      items_ready=dict(view.hud.items_ready), hp_pct=70, opp_name="Zed")
    specs = menu(inp)
    t = TacticalBrain().ask(inp)
    assert t is not None
    print(f"\n{kit.name} ({kit.role}) {len(specs)} moves: {[s.name for s in specs]}")
    print(f"  jev: {t.action} p={t.confidence:.2f} fits={t.menu_fits:.2f} {t.latency_ms:.0f} ms {t.tokens} tok  heads={t.extra}")
    print(f"  top actions: {dict(sorted(t.probabilities.items(), key=lambda kv: -kv[1])[:5])}")
    spec = {s.name: s for s in specs}[t.action]
    ctx.now = time.time()
    what = actions.execute(ctx, spec, t.cands, t.target_probs, t.where_probs, t.distance)
    print(f"  executed: '{what}' mode={mi.mode} orders={log}")
    # Every move must be executable without error on this scene.
    for s in specs:
        c2 = Controller(dry_run=True)
        m2 = Micro(c2, Screen(), keybinds.load(), "ORDER")
        m2.last_attack = 0.0
        ctx2 = actions.Ctx(mi=m2, sc=sc, kit=kit, lane=lane, now=time.time(), aspd=0.7, ally_units=[], lane_progress=0.48)
        actions.execute(ctx2, s, t.cands, t.target_probs, t.where_probs, t.distance)
    names = {s.name for s in specs}
    assert {"move", "attack_move", "stop", "hold", "attack"} <= names, names
    assert "flash" in names and "ignite" not in names  # no enemy champion on this frame, so ignite is filtered out
    assert "potion_slot2" in names and "ward" in names
print("MICRO OK")
