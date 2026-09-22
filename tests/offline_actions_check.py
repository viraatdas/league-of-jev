"""Every move in the action space executes, on a synthetic fight scene (enemy champion in range,
allied carry beside me, minions, all abilities and both summoners up), for both kits; then one
real Jev call on that scene to see what it picks with the whole menu."""
import time

from jev import actions, keybinds
from jev.control import Controller
from jev.kits import Thresh, Yasuo
from jev.lanes import Lane
from jev.micro import Micro, Scene, Track
from jev.screen import Screen
from jev.tactics import TacticalBrain, TacticInput, menu
from jev.vision import Unit

now = time.time()
me = (862.0, 490.0)
zed = Track(101, Unit("champion", "enemy", 990.0, 400.0, 0.28, (0, 0, 0, 0)), now)
minions = [Track(200 + i, Unit("minion", "enemy", 900.0 + 40 * i, 430.0 - 15 * i, 0.3 + 0.2 * i, (0, 0, 0, 0)), now) for i in range(4)]
carry = Unit("champion", "ally", 820.0, 540.0, 0.55, (0, 0, 0, 0))
sc = Scene(me_xy=me, minions=minions, allies=3, ally_champs=[carry], champ=zed, champ_dist=math_d if (math_d := ((990 - 862) ** 2 + (400 - 490) ** 2) ** 0.5 / 0.46) else None)
sc.ready = {k: True for k in "QWERDF"}
sc.r_lit = True
sc.killable_auto = [minions[0]]
items = [{"displayName": "Immortal Shieldbow", "slot": 0, "canUse": False},
         {"displayName": "Health Potion", "slot": 1, "canUse": True, "consumable": True},
         {"displayName": "Stealth Ward", "slot": 6, "canUse": True}]
api = {"me": {"level": 9, "hp_percent": 60}, "lane_opponent": {"champion": "Zed", "level": 8}}

for kit, lane in ((Yasuo(), Lane("mid", "ORDER")), (Thresh(), Lane("bot", "ORDER"))):
    if isinstance(kit, Thresh):
        kit.hook_at = now - 0.6  # a landed hook: Q is lit again for the recast
    mk = lambda log: Micro(Controller(dry_run=True, log=log.append), Screen(), keybinds.load(), "ORDER")
    log: list[str] = []
    mi = mk(log)
    ctx = actions.Ctx(mi=mi, sc=sc, kit=kit, lane=lane, now=now, aspd=0.9, ally_units=[carry], lane_progress=0.5)
    inp = TacticInput(ctx=ctx, api=api, plan={"intent": "trade", "lane": lane.name, "fight_favorable": 0.7},
                      ts=now, summoners=["flash", "ignite"], items=items, items_ready={0: True, 1: True, 6: True},
                      hp_pct=60, opp_name="Zed")
    specs = menu(inp)
    names = [s.name for s in specs]
    print(f"{kit.name}: {len(specs)} moves: {names}")
    cands = actions.candidates(ctx)
    for s in specs:
        l2: list[str] = []
        m2 = mk(l2)
        c2 = actions.Ctx(mi=m2, sc=sc, kit=kit, lane=lane, now=time.time(), aspd=0.9, ally_units=[carry], lane_progress=0.5,
                         summoners=["flash", "ignite"])
        what = actions.execute(c2, s, cands, {}, {}, 1.0)
        m2.run_due(time.time() + 1.0)  # fire queued combo steps
        assert what, f"{s.name} did nothing"
        assert s.mode or l2, f"{s.name} sent no input"
        print(f"  {s.name:15} -> {what:22} {[x for x in l2 if not x.startswith('move(')]}")
    want = {"Yasuo": {"Q", "W", "E", "E_then_Q", "beyblade", "R", "flash", "ignite", "attack", "ward", "potion_slot2"},
            "Thresh": {"Q2_fly", "fly_then_pull", "W", "E", "E_pull", "E_push", "R", "flash", "ignite", "attack", "ward"}}[kit.name]
    assert want <= set(names), want - set(names)
    t = TacticalBrain().ask(inp)
    print(f"  Jev on this fight: {t.action} p={t.confidence:.2f} heads={t.extra} ({t.latency_ms:.0f} ms)")
    print(f"  top: {dict(sorted(t.probabilities.items(), key=lambda kv: -kv[1])[:5])}\n")
print("ACTIONS OK")
