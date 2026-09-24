"""Pre-game check: every offline test, then the lane planner replayed on fixed recorded games and
compared with the last accepted numbers (tests/replay_baseline.json). A game is not started on a
build that fails it (jev session runs this when the code changed since the last pass).

Fails when, on any replayed game:
    last-hit targets that die within 1.5 s   drop more than 5 points
    the share of orders that are moves       rises more than 10 points
    E and E+Q orders                         fall under 70% of the baseline
    orders                                   rise over 150% of the baseline (order spam)

Run: uv run python scripts/pregame_check.py [--update-baseline] [--quick]
     --quick skips the tests that call Jev (TypeSafe) and need the network.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "scripts")
sys.path.insert(0, ".")

TESTS = ["offline_laneplan_check", "offline_vision_check", "offline_fight_check", "offline_teamfight_check",
         "offline_jungle_check", "offline_lanes_check", "offline_loop_check"]
NETWORK_TESTS = ["offline_actions_check", "offline_fighthead_check", "offline_micro_check"]
GAMES = [("g22_yasuo_beginner", "mid"), ("g24_yasuo_beginner", "mid"), ("g29_yasuo_top_beginner", "top")]
BASELINE = Path("tests/replay_baseline.json")


def run_tests(quick: bool) -> list[str]:
    failed = []
    for t in TESTS + ([] if quick else NETWORK_TESTS):
        r = subprocess.run([sys.executable, f"tests/{t}.py"], capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'} {t}")
        if not ok:
            failed.append(t)
            print("      " + "\n      ".join((r.stdout + r.stderr).strip().splitlines()[-6:]))
    return failed


def check(update: bool = False, quick: bool = False) -> bool:
    t0 = time.time()
    print("pre-game check: tests")
    failed = run_tests(quick)
    print("pre-game check: replays")
    from replay_plan import metrics

    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    now = {}
    problems = []
    for tag, lane in GAMES:
        if not Path(f"snapshots/night/{tag}/vision.jsonl").exists():
            print(f"  (no frames for {tag}: skipped)")
            continue
        m = now[tag] = metrics(tag, lane)
        b = base.get(tag)
        line = f"  {tag}: orders {m['orders']}, moves {m['moves']:.0%}, E {m['e']}, last hits {m['lasthits']} ({m['lasthit_died']:.0%} died on time)"
        if b:
            line += f"   [baseline: orders {b['orders']}, moves {b['moves']:.0%}, E {b['e']}, {b['lasthit_died']:.0%}]"
            if m["lasthit_died"] < b["lasthit_died"] - 0.05:
                problems.append(f"{tag}: last-hit targets dying on time {b['lasthit_died']:.0%} -> {m['lasthit_died']:.0%}")
            if m["moves"] > b["moves"] + 0.10:
                problems.append(f"{tag}: moves {b['moves']:.0%} -> {m['moves']:.0%} of orders")
            if b["e"] >= 5 and m["e"] < 0.7 * b["e"]:
                problems.append(f"{tag}: E orders {b['e']} -> {m['e']}")
            if m["orders"] > 1.5 * b["orders"]:
                problems.append(f"{tag}: orders {b['orders']} -> {m['orders']} (spam)")
        print(line)
    ok = not failed and not problems
    for p in problems:
        print("  REGRESSION " + p)
    if failed:
        print("  FAILED TESTS " + ", ".join(failed))
    if update or (ok and not base):
        BASELINE.write_text(json.dumps(now, indent=1))
        print(f"  baseline {'updated' if base else 'written'}: {BASELINE}")
    print(f"pre-game check: {'PASS' if ok else 'FAIL'} ({time.time() - t0:.0f} s)")
    return ok


if __name__ == "__main__":
    sys.exit(0 if check("--update-baseline" in sys.argv, "--quick" in sys.argv) else 1)
