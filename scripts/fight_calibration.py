"""Calibration of the fight head: does Jev's win_all_in predict fights we win, does in_danger
predict deaths? Reads logs/night/*.log.fights.jsonl (every applied read with its outcome three
seconds later). Run: uv run python scripts/fight_calibration.py [glob]"""
import glob
import json
import sys

paths = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "logs/night/*.log.fights.jsonl"))
rows = []
for p in paths:
    for line in open(p):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
print(f"{len(rows)} fight reads from {len(paths)} games")
if not rows:
    sys.exit(0)


def won(r):
    o = r["outcome"]
    return o["kills"] > 0 or (o["their_hp"] is not None and o["their_hp"] <= -0.25 and o["deaths"] == 0 and o["my_hp"] > -40)


def bucket(v):
    return min(4, int(v * 5))


for key, hit, label in (("win_all_in", won, "won the fight (kill, or took 25%+ off them and lived)"),
                        ("in_danger", lambda r: r["outcome"]["deaths"] > 0 or r["outcome"]["my_hp"] <= -35, "died or lost 35%+ HP"),
                        ("trade_worth", lambda r: r["outcome"]["their_hp"] is not None and r["outcome"]["their_hp"] * 100 < r["outcome"]["my_hp"],
                         "they lost more HP than me")):
    print(f"\n{key}: share that {label}")
    for b in range(5):
        sel = [r for r in rows if bucket(r[key]) == b]
        if sel:
            print(f"  {b / 5:.1f}-{(b + 1) / 5:.1f}: {sum(1 for r in sel if hit(r)) / len(sel):.2f}  (n={len(sel)})")
print("\nplans applied:", {p: sum(1 for r in rows if r["applied"] == p) for p in sorted({r["applied"] for r in rows})})
for p in ("all_in", "trade", "poke", "back_off", "escape"):
    sel = [r for r in rows if r["applied"] == p]
    if sel:
        mh = sum(r["outcome"]["my_hp"] for r in sel) / len(sel)
        th = [r["outcome"]["their_hp"] for r in sel if r["outcome"]["their_hp"] is not None]
        print(f"  {p:9} n={len(sel):4}  my HP {mh:+.1f}  their HP {100 * sum(th) / max(1, len(th)):+.1f}  "
              f"kills {sum(r['outcome']['kills'] for r in sel)}  deaths {sum(r['outcome']['deaths'] for r in sel)}")
