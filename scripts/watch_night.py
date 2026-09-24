"""Stream the interesting lines of the overnight loop (runner milestones and the current game's
fight / shop / dialog / last-hit / jungle events), one line per event, for a Monitor."""
import glob
import os
import re
import sys
import time

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
PAT = re.compile(r"order: .*(R Last|R kick|ignite|Flash)|fight\(jev\): (all_in|escape|retreat)|fight:|ward:|gank:|gank (top|mid|bot)|objective:|bleeding|shop \(no Jev\)|escape: Flash|shop: b|dialog|lasthits|smite|jungle: cleared|error|Error|Traceback|paused|===|surrender|score|game \d+:|deaths:|launch [^2]|launch failed")
seen: dict[str, int] = {}
while True:
    evs = sorted(glob.glob("logs/night/g*.log.events"), key=os.path.getmtime)
    for f in ["logs/night/night.out"] + evs[-1:]:
        if not os.path.exists(f):
            continue
        size = os.path.getsize(f)
        pos = seen.setdefault(f, size)
        if size > pos:
            with open(f, errors="ignore") as fh:
                fh.seek(pos)
                for line in fh.read().splitlines():
                    if PAT.search(line):
                        print(line[:220], flush=True)
        seen[f] = size
    time.sleep(5)
