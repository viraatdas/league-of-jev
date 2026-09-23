"""Game report from a play log (one status line per second): deaths with their game times,
KDA, CS per minute, and how much of the game the bot was blind, paused or dead."""
from __future__ import annotations

import collections
import re
from pathlib import Path


def _gt(s: str) -> float:
    m, sec = s.split(":")
    return int(m) * 60 + int(sec)


def report(path: str | Path) -> str:
    lines = [l for l in Path(path).read_text(errors="ignore").splitlines() if " t=" in l and " | " in l]
    if not lines:
        return "no status lines yet"
    games, cur, last_t = [], [], -1.0
    for l in lines:
        m = re.search(r" t=(\d+:\d+) ", l)
        if not m:
            continue
        t = _gt(m.group(1))
        if t + 30 < last_t and cur:  # game time went backwards: a new game
            games.append(cur)
            cur = []
        cur.append((t, l))
        last_t = t
    if cur:
        games.append(cur)
    out = []
    for gi, g in enumerate(games, 1):
        deaths, prev_d = [], None
        kda = cs = "?"
        for t, l in g:
            m = re.search(r" (\d+)/(\d+)/(\d+) cs=(\d+)", l)
            if m:
                d = int(m.group(2))
                if prev_d is not None and d > prev_d:
                    deaths.append(t)
                prev_d = d
                kda, cs = f"{m.group(1)}/{m.group(2)}/{m.group(3)}", int(m.group(4))
        n = len(g)
        span = max(1.0, g[-1][0] - g[0][0])
        cnt = lambda pat: sum(1 for _, l in g if pat in l)
        intents = collections.Counter(m.group(1) for _, l in g if (m := re.search(r"intent=(\w+)", l)))
        fmt = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"
        out.append(
            f"game {gi}: {fmt(g[0][0])} -> {fmt(g[-1][0])}  KDA {kda}  CS {cs} ({(cs if isinstance(cs, int) else 0) / (span / 60):.1f}/min)\n"
            f"  deaths: {len(deaths)} at {', '.join(fmt(d) for d in deaths) or '-'}\n"
            f"  input paused {cnt('keys=n') * 100 // n}%   position unknown {cnt('pos=?') * 100 // n}%   dead {intents.get('dead', 0) * 100 // n}%\n"
            f"  time by intent: " + ", ".join(f"{k} {v * 100 // n}%" for k, v in intents.most_common(6))
        )
    return "\n".join(out)
