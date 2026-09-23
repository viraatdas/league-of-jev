"""Fight review for one overnight game: every stretch where the micro layer ran a fight mode
(trade / all_in) becomes an episode with our HP and the enemy champion's HP before and after,
the orders given, and a contact sheet of six frames.

Run: uv run python scripts/fight_review.py g04_yasuo   -> logs/night/g04_yasuo_fights.md
"""
import json
import os
import re
import sys

import cv2
import numpy as np

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
tag = sys.argv[1]
log = f"logs/night/{tag}.log"
fdir = f"snapshots/night/{tag}"


def secs(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


status = []  # (wall seconds, game time, hp, micro, kda)
for line in open(log, errors="ignore"):
    m = re.match(r"(\d\d:\d\d:\d\d) t=(\d+:\d+) L\d+ hp=(\d+)% gold=\d+ (\d+/\d+/\d+)", line)
    if not m:
        continue
    mi = re.search(r"micro: ([^|]*)", line)
    status.append((secs(m.group(1)), m.group(2), int(m.group(3)), (mi.group(1).strip() if mi else ""), m.group(4)))

episodes, cur = [], None
for w, gt, hp, micro, kda in status:
    fighting = micro.startswith(("trade:", "all_in:")) or any(k in micro for k in (
        "ignite", "R Last Breath", "Q3 tornado", "E+Q", "the champion", "R kick", "Sonic Wave", "Q2 dash", "escape"))
    if fighting and (cur is None or w - cur["end"] > 3):
        cur = {"start": w, "end": w, "gt": gt, "hp0": hp, "kda0": kda, "orders": []}
        episodes.append(cur)
    if cur is not None and w - cur["end"] <= 3 and fighting:
        cur["end"] = w
        if micro not in cur["orders"]:
            cur["orders"].append(micro)
for ep in episodes:
    after = [s for s in status if ep["end"] + 2 <= s[0] <= ep["end"] + 6]
    ep["hp1"] = after[-1][2] if after else None
    ep["kda1"] = after[-1][4] if after else ep["kda0"]

vision = []
vp = os.path.join(fdir, "vision.jsonl")
if os.path.exists(vp):
    for line in open(vp):
        r = json.loads(line)
        hh, mm, ss = r["f"][0:2], r["f"][2:4], r["f"][4:6]
        vision.append((int(hh) * 3600 + int(mm) * 60 + int(ss), r))

frames = sorted(f for f in os.listdir(fdir) if f.endswith((".jpg", ".png")) and f[:9].isdigit())


def frame_secs(f: str) -> int:
    return int(f[0:2]) * 3600 + int(f[2:4]) * 60 + int(f[4:6])


out = [f"# Fights in {tag}", "", "| # | game time | length | orders | my HP | enemy HP | KDA |", "|---|---|---|---|---|---|---|"]
for i, ep in enumerate(episodes, 1):
    win = [r for w, r in vision if ep["start"] - 1 <= w <= ep["end"] + 4]
    ehp = [u[4] for r in win for u in r["units"] if u[0] == "champion" and u[1] == "enemy"]
    e0 = f"{ehp[0] * 100:.0f}%" if ehp else "?"
    e1 = f"{ehp[-1] * 100:.0f}%" if ehp else "?"
    orders = "; ".join(o.split(": ", 1)[-1] for o in ep["orders"])[:120]
    out.append(f"| {i} | {ep['gt']} | {ep['end'] - ep['start'] + 1}s | {orders} | {ep['hp0']}% -> {ep['hp1']}% | {e0} -> {e1} | {ep['kda0']} -> {ep['kda1']} |")
    fs = [f for f in frames if ep["start"] - 1 <= frame_secs(f) <= ep["end"] + 3]
    if fs:
        pick = [fs[j] for j in np.linspace(0, len(fs) - 1, min(6, len(fs))).astype(int)]
        ims = []
        for f in pick:
            im = cv2.imread(os.path.join(fdir, f))[60:900, 200:1500]
            cv2.putText(im, f.split("_")[1][:5] if "_" in f else f, (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 255), 3)
            ims.append(cv2.resize(im, (im.shape[1] * 2 // 5, im.shape[0] * 2 // 5)))
        while len(ims) % 3:
            ims.append(np.zeros_like(ims[0]))
        sheet = np.vstack([np.hstack(ims[k:k + 3]) for k in range(0, len(ims), 3)])
        cv2.imwrite(os.path.join(fdir, f"fight_{i:02d}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 80])
open(f"logs/night/{tag}_fights.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
