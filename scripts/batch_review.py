"""A batch of games in one table: kills, deaths, assists, K/D, CS and level at 10:00, deaths before 15:00,
the result when the end screen showed it. For the per-game detail: scripts/game_review.py <tag>.

Run: uv run python scripts/batch_review.py g37        (every game from g37 on)
"""
import glob
import os
import re
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))

first = int(re.sub(r"\D", "", sys.argv[1])) if len(sys.argv) > 1 else 0
STATUS = re.compile(r"t=(\d+):(\d+) L(\d+) hp=(\d+)% gold=(\d+) (\d+)/(\d+)/(\d+) cs=(\d+)")
rows = []
for log in sorted(glob.glob("logs/night/g*_*.log"), key=lambda p: int(re.match(r".*/g(\d+)_", p).group(1))):
    n = int(re.match(r".*/g(\d+)_", log).group(1))
    if n < first:
        continue
    last, at10, deaths15 = None, None, 0
    prev_d = 0
    for line in open(log, errors="ignore"):
        m = STATUS.search(line)
        if not m:
            continue
        gt = int(m.group(1)) * 60 + int(m.group(2))
        k, d, a = int(m.group(6)), int(m.group(7)), int(m.group(8))
        cur = (gt, int(m.group(3)), k, d, a, int(m.group(9)))
        if d > prev_d and gt < 900:
            deaths15 += d - prev_d
        prev_d = max(prev_d, d)
        if at10 is None and gt >= 600:
            at10 = cur
        last = cur
    if last is None:
        continue
    result = "?"
    out = "logs/night/night.out"
    tag = os.path.basename(log)[:-4]
    rows.append((tag, last, at10, deaths15, result))

K = D = A = 0
print(f"{'game':28s} {'time':>6s} {'K/D/A':>9s} {'lvl':>4s} {'CS':>4s}  {'@10 lvl/CS':>10s}  {'deaths<15':>9s}")
for tag, last, at10, d15, result in rows:
    gt, lvl, k, d, a, cs = last
    K, D, A = K + k, D + d, A + a
    a10 = f"{at10[1]}/{at10[5]}" if at10 else "-"
    print(f"{tag:28s} {gt // 60:3d}:{gt % 60:02d} {k:3d}/{d}/{a:<3d} {lvl:4d} {cs:4d}  {a10:>10s}  {d15:9d}")
if rows:
    print(f"\n{len(rows)} games: {K}/{D}/{A}, K/D {K / max(1, D):.2f}, KDA {(K + A) / max(1, D):.2f}, "
          f"kills/game {K / len(rows):.1f}, deaths/game {D / len(rows):.1f}")
