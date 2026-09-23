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


rows = []
for log in sorted(glob.glob("logs/night/g*_*.log"), key=lambda p: int(re.search(r"g(\d+)_", p).group(1))):
    tag = os.path.basename(log)[:-4]
    st = last_status(log) or ""
    m = re.search(r" t=(\d+:\d+) L(\d+) .*? (\d+)/(\d+)/(\d+) cs=(\d+)", st)
    t, lvl, k, d, a, cs = (m.groups() if m else ("?", "?", "?", "?", "?", "?"))
    ev = open(log + ".events", errors="ignore").read() if os.path.exists(log + ".events") else ""
    fights = len(re.findall(r"fight: (kill window|trade window|strategy says)", ev))
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
    rows.append(f"{tag:14} t={t:>5} L{lvl:>2} {k}/{d}/{a:<3} cs~{cs:>3}  fights {fights} (lost {losing})  lasthits {lhs or '-':18} "
                f"shop {bought} ok/{failed} fail  dialogs {dialogs}  stray shop {stray}"
                + (f"  camps {cleared} smites {smites}" if "leesin" in tag else ""))
print("\n".join(rows))
