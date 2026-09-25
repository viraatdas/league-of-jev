"""One game, everything the harness logged about it, on one screen: result, CS at 10:00, time per
intent, each death with its context, power plays and what they took, the lane planner's picks, what
the learner learned, what was read about the enemy, ganks, last-hit rates.

Run: uv run python scripts/game_review.py g30_yasuo_top_beginner      (or the newest game with no argument)
"""
import collections
import glob
import json
import os
import re
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

if len(sys.argv) > 1:
    tag = sys.argv[1]
else:
    logs = sorted(glob.glob("logs/night/g*_*.log"), key=os.path.getmtime)
    tag = os.path.basename(logs[-1])[:-4]
log = f"logs/night/{tag}.log"
events = [line.rstrip("\n") for line in open(f"{log}.events", errors="ignore")] if os.path.exists(f"{log}.events") else []
status = [line for line in open(log, errors="ignore") if " t=" in line and " | " in line]

STATUS = re.compile(r"(\d\d:\d\d:\d\d) t=(\d+):(\d+) L(\d+) hp=(\d+)% gold=(\d+) (\d+/\d+/\d+) cs=(\d+) \| (\w+) .*?intent=(\w+)")
rows = []
for line in status:
    m = STATUS.search(line)
    if m:
        rows.append({"wall": m.group(1), "gt": int(m.group(2)) * 60 + int(m.group(3)), "lvl": int(m.group(4)),
                     "hp": int(m.group(5)), "gold": int(m.group(6)), "kda": m.group(7), "cs": int(m.group(8)),
                     "phase": m.group(9), "intent": m.group(10), "line": line})
if not rows:
    sys.exit(f"no status lines in {log}")
last = rows[-1]
print(f"== {tag}: {last['gt'] // 60}:{last['gt'] % 60:02d}, level {last['lvl']}, KDA {last['kda']}, CS {last['cs']}")
at10 = next((r for r in rows if r["gt"] >= 600), None)
if at10:
    print(f"   at 10:00: level {at10['lvl']}, CS {at10['cs']} (the API counts CS in tens), KDA {at10['kda']}")

# Time per intent, before and after 15:00
for label, part in (("before 15:00", [r for r in rows if r["gt"] < 900]), ("after 15:00", [r for r in rows if r["gt"] >= 900])):
    if part:
        c = collections.Counter(r["intent"] for r in part)
        print(f"   {label:12s}: " + "  ".join(f"{k} {v / len(part):.0%}" for k, v in c.most_common(7)))

# Deaths
print("\n-- deaths")
prev_dead = False
for i, r in enumerate(rows):
    dead = r["intent"] == "dead"
    if dead and not prev_dead:
        win = rows[max(0, i - 8):i]
        hp = "->".join(str(w["hp"]) for w in win[::2])
        intents = "/".join(dict.fromkeys(w["intent"] for w in win))
        champs = sum(1 for w in win if re.search(r"champ \d+% @", w["line"]))
        acts = [re.search(r"act=([^|]*?) keys", w["line"]) for w in win[-3:]]
        print(f"   {r['gt'] // 60}:{r['gt'] % 60:02d} hp {hp} | {intents} | champion on screen {champs}/{len(win)} s | "
              f"{'; '.join(a.group(1).strip()[:30] for a in acts if a)}")
    prev_dead = dead

# Macro: power plays, objectives, towers
print("\n-- macro")
pp = [e for e in events if "macro: power play" in e]
objs = collections.Counter(re.sub(r"\(.*", "", e.split("objective: ", 1)[1]).strip() for e in events if "objective: " in e)
towers = [e for e in events if "towers (minimap)" in e or "tower killed (event)" in e]
print(f"   power plays: {len(pp)}; objective plans: " + (", ".join(f"{k} x{v}" for k, v in objs.most_common(8)) or "none"))
for e in towers[:14]:
    print("   " + e)

# Lane planner picks
plan_f = f"{log}.plan.jsonl"
if os.path.exists(plan_f):
    picks = collections.Counter()
    for line in open(plan_f):
        try:
            picks[json.loads(line)["pick"]["kind"]] += 1
        except Exception:
            pass
    tot = sum(picks.values())
    print(f"\n-- lane planner ({tot} logged picks, holds not logged): " + "  ".join(f"{k} {v} ({v / tot:.0%})" for k, v in picks.most_common()))

# Learning, enemy knowledge, last hits
learned = [e for e in events if " learned: " in e]
forecast = [e for e in events if "hp forecast:" in e]
names = collections.Counter(e.split("on screen: ", 1)[1] for e in events if "on screen: " in e)
spells = [e for e in events if " used at " in e and "down ~" in e]
lh = [e for e in events if "lasthits paid:" in e]
print("\n-- learning and knowledge")
if learned:
    print("   " + learned[-1].split(" ", 1)[1])
if forecast:
    print("   " + forecast[-1].split(" ", 1)[1])
print(f"   champions read on screen: {dict(names)}; her spells seen used: {len(spells)}")
skills = [e for e in events if "skillshots on champions" in e]
if skills:
    print("   " + skills[-1].split(" ", 1)[1])
for e in spells[:5]:
    print("     " + e)
if lh:
    tot = {}
    for part in lh[-1].split("lasthits paid: ", 1)[1].split(", "):
        m = re.match(r"(Q|E|auto) (\d+)/(\d+)$", part.strip())
        if m:
            tot[m.group(1)] = f"{m.group(2)}/{m.group(3)}"
    print(f"   last hits paid: {tot}")

# Ganks (Lee)
ganks = [e for e in events if re.search(r"gank \w+: ", e)]
if ganks:
    starts = sum(1 for e in ganks if "their laner at" in e)
    ends = collections.Counter(re.search(r"over \(([^)]*)\)", e).group(1) for e in ganks if "over (" in e)
    print(f"\n-- ganks: {starts} started; ended: {dict(ends)}")

# Fights
orders = collections.Counter(re.sub(r"\(.*|\d+%", "", e.split("order: ", 1)[1]).strip() for e in events if "order: " in e)
if orders:
    print("\n-- fight orders: " + "  ".join(f"{k} {v}" for k, v in orders.most_common(12)))
