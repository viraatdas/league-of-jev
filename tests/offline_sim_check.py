"""Lee Sin's go/no-go and combo against simulated targets (scripts/sim_lee.py): no fight started where
it cannot be won (a tank, a full-HP bruiser that hits hard, a healthy one trading back), a kill in nearly
every fight on a farming or hurt target, most fights on runners, and Lee never dying in a fight he chose.
Run: uv run python tests/offline_sim_check.py"""
import os
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "scripts")
import sim_lee  # noqa: E402

out = sim_lee.run_all(seeds=12)
no_go = ["tank 90% (1600 HP, 90 armor)", "full HP bruiser, hits hard", "trades back 90%, hits hard"]
sure = ["farming bot 80%", "hurt 50%, Flash"]
most = ["walking off 80%", "zig-zag 70%", "running 60% + our minion by her"]
for k in no_go:
    assert out[k]["go"] == 0.0, (k, out[k])
for k in sure:
    assert out[k]["go"] == 1.0 and out[k]["kill_when_go"] >= 0.9, (k, out[k])
for k in most:
    assert out[k]["go"] >= 0.9 and out[k]["kill_when_go"] >= 0.5, (k, out[k])
for k, o in out.items():
    assert not o["died_when_go"], (k, o)
print("SIM OK (Lee go and combo)")
