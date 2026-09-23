"""What happened in the last N seconds of play, for a quick human (or Claude) review: deaths and
KDA/CS change, moves executed and whether damaging ones took HP off their target, last-hit
attempts against CS gained, and how much of the window the bot was paused or blind."""
from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path

DAMAGING = {"Q", "E_then_Q", "beyblade", "R", "ignite", "attack", "E", "W"}


def digest(play_log: str | Path, decisions: str | Path, since_s: float = 90.0) -> str:
    now = time.time()
    rows = []
    for l in Path(play_log).read_text(errors="ignore").splitlines()[-int(since_s * 2) - 5:]:
        m = re.match(r"(\d\d):(\d\d):(\d\d) t=(\d+:\d+) ", l)
        if m:
            rows.append(l)
    rows = rows[-int(since_s):]
    if not rows:
        return "no play lines in the window"
    first, last = rows[0], rows[-1]
    kda = lambda l: (re.search(r" (\d+)/(\d+)/(\d+) cs=(\d+)", l) or [None])[0]
    k0, k1 = kda(first), kda(last)
    gt = lambda l: (re.search(r"t=(\S+)", l) or re.search(r"(\S+)", l)).group(1)
    out = [f"window {gt(first)} -> {gt(last)}: {k0 or '?'} -> {k1 or '?'}"]
    paused = sum("keys=n" in l for l in rows) * 100 // len(rows)
    blind = sum("pos=?" in l for l in rows) * 100 // len(rows)
    intents = collections.Counter(m.group(1) for l in rows if (m := re.search(r"intent=(\w+)", l)))
    out.append(f"paused {paused}%  minimap-blind {blind}%  intents " + ", ".join(f"{k} {v}" for k, v in intents.most_common(4)))
    acts = collections.Counter(m.group(1).strip() for l in rows if (m := re.search(r"micro: ([^|]+?) \|", l)))
    if acts:
        out.append("micro actions seen: " + ", ".join(f"{k} x{v}" for k, v in acts.most_common(6)))
    try:
        ds = [json.loads(x) for x in Path(decisions).read_text().splitlines() if x.strip()]
    except FileNotFoundError:
        ds = []
    ds = [d for d in ds if now - d.get("ts", 0) <= since_s + 5]
    exe = [d for d in ds if d.get("executed")]
    out.append(f"tactical answers {len(ds)}, executed one-shots {len(exe)}, explored {sum(d.get('explored', False) for d in ds)}")
    by = collections.defaultdict(list)
    for d in exe:
        by[d["action"]].append(d)
    for a, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        dmg = [d["outcome"].get("enemy_hp") for d in lst if d["outcome"].get("enemy_hp") is not None]
        landed = sum(1 for x in dmg if x <= -0.03)
        extra = f", took HP off the enemy champion {landed}/{len(dmg)}" if (a in DAMAGING and dmg) else ""
        out.append(f"  {a} x{len(lst)}{extra}, mean score {sum(d['score'] for d in lst) / len(lst):+.2f}")
    return "\n".join(out)
