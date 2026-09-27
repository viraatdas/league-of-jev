"""Yasuo's go/no-go and combo against simulated champions (scripts/sim_yasuo.py): fights started with the
tornado ready (or on a hurt target) end in kills; none started on a tank, a full-HP bruiser that hits
hard, or a target he cannot reach; the combo kills farming and hurt targets when forced; Yasuo never
dies in a fight he chose.
Run: uv run python tests/offline_sim_yasuo_check.py"""
import os
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "scripts")
import sim_yasuo  # noqa: E402

out = sim_yasuo.run_all(seeds=12)
for k in ("walking off 70%, Q3 ready", "far 70%, Q3 ready", "hurt 45%, Flash"):
    assert out[k]["go"] == 1.0 and out[k]["kill_when_go"] >= 0.9, (k, out[k])
assert out["zig-zag 70%, Q3 ready"]["go"] == 1.0 and out["zig-zag 70%, Q3 ready"]["kill_when_go"] >= 0.6
for k in ("full HP bruiser, hits hard", "tank 90% (1600 HP, 90 armor)", "no wave (no E targets) 70%"):
    assert out[k]["go"] == 0.0, (k, out[k])
for k in ("farming bot 80%", "hurt 45%, Flash", "far, tornado range 70%"):
    assert out[k]["kill"] >= 0.9, (k, out[k])
for k, o in out.items():
    assert not o["died_when_go"], (k, o)
print("SIM OK (Yasuo go and combo)")
