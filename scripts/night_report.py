"""One line per overnight game: result, CS, deaths, fights, last-hit rates, shop and dialog
counts, from logs/night/gNN_*. Run: uv run python scripts/night_report.py"""
import glob
import os
import re

os.chdir(os.path.join(os.path.dirname(__file__), ".."))


def last_status(path):
    last = None
    with open(path, errors="ignore") as f:
        for line in f:
            if " t=" in line and " | " in line:
                last = line
    return last


def results() -> list[tuple[float, float, bool, str]]:
    """(start epoch, minutes, win, kda) of recent games from the client's match history."""
    try:
        from datetime import datetime

        from jev.lcu import LCU

        code, body = LCU().req("GET", "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=40")
        out = []
        for g in (body or {}).get("games", {}).get("games", []):
            st = g["participants"][0]["stats"]
            t0 = datetime.fromisoformat(g["gameCreationDate"].replace("Z", "+00:00")).timestamp()
            out.append((t0, g.get("gameDuration", 0) / 60, bool(st.get("win")), f"{st.get('kills')}/{st.get('deaths')}/{st.get('assists')}"))
        return out
    except Exception:  # noqa: BLE001  client closed: no results column
        return []


RESULTS = results()


def result_for(log: str) -> str:
    """Win or loss of the game whose start is closest to this log's first line (within 6 minutes)."""
    import time as _t

    try:
        first = open(log, errors="ignore").readline()[:8]
        day = _t.strftime("%Y-%m-%d", _t.localtime(os.path.getmtime(log)))
        start = _t.mktime(_t.strptime(f"{day} {first}", "%Y-%m-%d %H:%M:%S"))
    except (OSError, ValueError):
        return "   "
    best = min(RESULTS, key=lambda r: abs(r[0] - start), default=None)
    if best is None or abs(best[0] - start) > 360:
        return "   "
    return "WIN" if best[2] else "los"


rows = []
for log in sorted(glob.glob("logs/night/g*_*.log"), key=lambda p: int(re.search(r"g(\d+)_", p).group(1))):
    tag = os.path.basename(log)[:-4]
    st = last_status(log) or ""
    m = re.search(r" t=(\d+:\d+) L(\d+) .*? (\d+)/(\d+)/(\d+) cs=(\d+)", st)
    t, lvl, k, d, a, cs = (m.groups() if m else ("?", "?", "?", "?", "?", "?"))
    ev = open(log + ".events", errors="ignore").read() if os.path.exists(log + ".events") else ""
    fights = len(re.findall(r"fight: (kill window|trade window|strategy says|team fight|kill pressure|under our tower)", ev))
    executes = len(re.findall(r"fight: execute", ev))
    losing = len(re.findall(r"fight: losing", ev))
    bought = len(re.findall(r"shop: bought", ev))
    failed = len(re.findall(r"shop: could not buy", ev))
    dialogs = len(re.findall(r"dialog: clicked", ev))
    stray = len(re.findall(r"shop: closed a shop left open", ev))
    smites = len(re.findall(r"smite at", ev))
    cleared = len(re.findall(r"jungle: cleared", ev))
    lh = re.findall(r"lasthits paid: (.*)", ev)
    q = au = (0, 0)
    tot = {"Q": [0, 0], "auto": [0, 0]}
    # each 'lasthits paid' line is cumulative per harness run: sum the last line of each run
    runs = ev.split("capture: screencapturekit")
    for r in runs:
        ls = re.findall(r"lasthits paid: (.*)", r)
        if ls:
            for kind in ("Q", "auto"):
                mm = re.search(rf"(?:^|, ){kind} (\d+)/(\d+)", ls[-1])
                if mm:
                    tot[kind][0] += int(mm.group(1))
                    tot[kind][1] += int(mm.group(2))
    lhs = " ".join(f"{k} {v[0]}/{v[1]}" for k, v in tot.items() if v[1])
    rows.append(f"{tag:22} {result_for(log)} t={t:>5} L{lvl:>2} {k}/{d}/{a:<3} cs~{cs:>3}  fights {fights} exec {executes} (lost {losing})  lasthits {lhs or '-':18} "
                f"shop {bought} ok/{failed} fail  dialogs {dialogs}  stray shop {stray}"
                + (f"  camps {cleared} smites {smites}" if "leesin" in tag else ""))
print("\n".join(rows))
