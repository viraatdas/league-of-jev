"""Yasuo's fight code against simulated champions (scripts/sim_yasuo.py): kills on a farming or hurt
target, the tornado-and-R kill on a runner when the fight starts with the tornado ready, R used on the
knock-up, and no death in the fights simulated.
Run: uv run python tests/offline_sim_yasuo_check.py"""
import os
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "scripts")
import sim_yasuo  # noqa: E402

out = sim_yasuo.run_all(seeds=12)
for k in ("farming bot 80%", "hurt 45%, Flash", "far, tornado range 70%"):
    assert out[k]["kill"] >= 0.9, (k, out[k])
for k in ("walking off 70%, Q3 ready", "far 70%, Q3 ready"):
    assert out[k]["kill"] >= 0.9 and out[k]["r"] >= 0.8, (k, out[k])
assert out["zig-zag 70%, Q3 ready"]["kill"] >= 0.6, out["zig-zag 70%, Q3 ready"]
for k, o in out.items():
    assert o["died"] == 0.0, (k, o)
print("SIM OK (Yasuo combo)")
