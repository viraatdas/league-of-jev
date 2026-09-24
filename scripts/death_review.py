"""Every death in one game: the eight seconds before it, one line per second (HP, what vision saw,
the micro action, the intent), plus the fight/escape events in that window.

Run: uv run python scripts/death_review.py g17_yasuo   -> prints; logs/night/g17_yasuo_deaths.md
"""
import os
import re
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
tag = sys.argv[1]
log = f"logs/night/{tag}.log"
events = f"logs/night/{tag}.log.events"


def secs(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


rows = []
for line in open(log, errors="ignore"):
    m = re.match(r"(\d\d:\d\d:\d\d) t=(\d+:\d+) L(\d+) hp=(\d+)% gold=\d+ (\d+)/(\d+)/(\d+)", line)
    if not m:
        continue
    scr = re.search(r"screen: ([^/|]*)", line)
    mic = re.search(r"micro: ([^|]*)", line)
    it = re.search(r"intent=(\w+)", line)
    rows.append({"w": secs(m.group(1)), "clock": m.group(1), "gt": m.group(2), "lvl": int(m.group(3)), "hp": int(m.group(4)),
                 "deaths": int(m.group(6)), "screen": (scr.group(1).strip() if scr else ""), "micro": (mic.group(1).strip() if mic else ""),
                 "intent": it.group(1) if it else ""})
ev = []
if os.path.exists(events):
    for line in open(events, errors="ignore"):
        m = re.match(r"(\d\d:\d\d:\d\d) (.*)", line)
        if m and re.search(r"fight|escape|bleed|gank|retreat|order: (all_in|trade|escape)|execute|objective", m.group(2)):
            ev.append((secs(m.group(1)), m.group(1), m.group(2)[:110]))
out = [f"# Deaths in {tag}", ""]
prev = 0
for i, r in enumerate(rows):
    if r["deaths"] > prev:
        prev = r["deaths"]
        win = [x for x in rows[max(0, i - 40):i + 1] if r["w"] - 8 <= x["w"] <= r["w"]]
        out.append(f"## death {r['deaths']} at {r['gt']} (level {r['lvl']})")
        for x in win:
            out.append(f"  {x['gt']:>6} hp {x['hp']:>3}%  {x['intent']:<10} {x['micro'][:48]:<48} {x['screen'][:70]}")
        for w, c, e in ev:
            if r["w"] - 10 <= w <= r["w"]:
                out.append(f"  ! {c} {e}")
        out.append("")
open(f"logs/night/{tag}_deaths.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
