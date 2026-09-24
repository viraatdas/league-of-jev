"""Replay a recorded game's frames through Yasuo's lane layer, the lane planner and the old rules
side by side, and count what each would have ordered: moves, autos, Q, E, E+Q, hits on the
champion. No game, no Jev call; the frames and the HUD are the ones the bot saw live.

Run: uv run python scripts/replay_plan.py g24_yasuo_beginner [mid|top|bot] [--show N] [--kind q]
"""
import collections
import json
import os
import sys
import time

import cv2

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

from jev import config, keybinds  # noqa: E402
from jev.control import Controller  # noqa: E402
from jev.kits import Yasuo  # noqa: E402
from jev.lanes import Lane  # noqa: E402
from jev.micro import Micro, UnitTracker, build_scene  # noqa: E402
from jev.minimap import MinimapReader  # noqa: E402
from jev.screen import Screen  # noqa: E402
from jev.vision import Unit, View, VisionReader  # noqa: E402


def wall(f: str) -> float:
    return int(f[:2]) * 3600 + int(f[2:4]) * 60 + int(f[4:6]) + int(f[6:9]) / 1000


_readers: dict = {}


def run(tag: str, lane_name: str, planner: bool) -> tuple[collections.Counter, list]:
    """Replay `tag`'s frames through Yasuo's lane layer (the planner or the old rules). Counts of
    order kinds, plus "_died": (kind, target died within 1.5 s) -> n."""
    if not _readers:
        _readers["screen"] = Screen()
        _readers["vr"], _readers["mr"] = VisionReader(), MinimapReader(_readers["screen"])
    screen, vr, mr = _readers["screen"], _readers["vr"], _readers["mr"]
    fdir = f"snapshots/night/{tag}"
    rows = [json.loads(line) for line in open(f"{fdir}/vision.jsonl")]
    lane = Lane(lane_name, "ORDER")
    x0, y0, side = config.GEOMETRY.minimap
    saved = config.FAST.lane_planner
    config.FAST.lane_planner = planner
    kit = Yasuo()
    mi = Micro(Controller(dry_run=True, log=lambda m: None), screen, keybinds.load(), "ORDER")
    mins, champs = UnitTracker(), UnitTracker(max_jump_px=90)
    counts, picks = collections.Counter(), []
    t_prev = None
    cd = {"Q": 0.0, "E": 0.0}  # the replay's own cooldowns: the recorded HUD knows nothing of our casts
    last_seen, hits = {}, []     # did the minion we went for die within 1.5 s? (a premature hit if not)
    for r in rows:
        units = [Unit(k, team, x, y, hp, tuple(bar)) for k, team, x, y, hp, bar in r["units"]]
        enemies = [u for u in units if u.team == "enemy" and u.kind == "minion"]
        if not enemies or r["me"] is None:
            continue
        t = wall(r["f"])
        if t_prev is not None and t - t_prev > 1.5:
            mins, champs = UnitTracker(), UnitTracker(max_jump_px=90)  # a gap: tracks would lie
        t_prev = t
        path = f"{fdir}/{r['f']}.jpg" if os.path.exists(f"{fdir}/{r['f']}.jpg") else f"{fdir}/{r['f']}.png"
        img = cv2.imread(path)
        if img is None:
            continue
        bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        hud = vr.read_hud(bgra)
        for key in ("Q", "E"):
            hud.ready[key] = bool(hud.ready.get(key)) and t >= cd[key]
        k, team, x, y, hp, bar = r["me"]
        me = Unit(k, team, x, y, hp, tuple(bar))
        view = View(units=units, me=me, hud=hud, ts=t)
        mm = mr.read(bgra[y0:y0 + side, x0:x0 + side])
        fwd = lane.screen_dir(lane.project(mm.pos)[0]) if mm.pos else mi.fwd
        mi.fwd = fwd
        gmin = r["t"] / 60
        ad = 68 + 3.2 * gmin + (15 if gmin > 10 else 0)
        rank = max(1, min(5, 1 + int(gmin // 3)))
        mtr = mins.update(enemies, t)
        for tr in mtr:
            last_seen[tr.id] = t
        ctr = champs.update([u for u in units if u.team == "enemy" and u.kind == "champion"], t)
        sc = build_scene(view, mtr, ctr, ad, rank, r["t"], t, config.GEOMETRY.champion_px, aspd=0.75, fwd=fwd,
                         e_dmg=kit.e_minion_damage(rank, ad, 1 + int(gmin // 1.5)))
        sc.lane = {"opp_range": 550.0, "opp_hp": 640 + 95 * min(17, int(gmin // 1.5)), "aggression": 1.0}
        mi.hp_pct = me.hp * 100
        kit.q.hud = hud.q3
        mi.last_order = 0.0
        before = (mi.orders, mi.last_move)
        kit.continuous(mi, sc, t, 0.75, "farm", False)
        if mi.orders == before[0] and mi.last_move == before[1]:
            counts["hold / nothing"] += 1
            continue
        a = mi.last_action
        if "Q" in a and not a.startswith("plan: stand"):
            cd["Q"] = t + 3.3
        if (a.startswith("E") or "E+Q" in a) and not a.startswith("plan: stand"):
            cd["E"] = t + 0.5
        kind = ("move" if (a.startswith("farm:") or a.startswith("plan: stand") or a == "back off")
                else "E+Q" if "E+Q" in a else "E" if a.startswith("E") else "Q" if "Q" in a
                else "champion" if "champion" in a else "last hit" if a.startswith("last hit") else "auto (push)")
        counts[kind] += 1
        if kind in ("last hit", "Q", "E", "E+Q") and ("last hit" in a):
            tid = max(mi.attacked_ids, key=mi.attacked_ids.get) if mi.attacked_ids else None
            if tid is not None and mi.attacked_ids[tid] == t:
                hits.append((kind, tid, t))
        if planner and mi.plan_log:
            picks.append((r["f"], mi.plan_log[-1]))
    died = collections.Counter()
    for kind, tid, t in hits:
        died[(kind, last_seen.get(tid, t) - t <= 1.5)] += 1
    counts["_died"] = died
    config.FAST.lane_planner = saved
    return counts, picks


def metrics(tag: str, lane_name: str | None = None) -> dict:
    """The planner's numbers on one recorded game (the pre-game check compares these)."""
    lane_name = lane_name or ("top" if "_top" in tag else "mid")
    c, _ = run(tag, lane_name, True)
    died = c.pop("_died")
    tot = sum(v for k, v in c.items() if k != "hold / nothing")
    ok = sum(v for (k, d), v in died.items() if d)
    tried = sum(died.values())
    return {"orders": tot, "moves": round(c["move"] / max(1, tot), 3), "e": c["E"] + c["E+Q"],
            "lasthits": tried, "lasthit_died": round(ok / max(1, tried), 3)}


if __name__ == "__main__":
    tag = sys.argv[1]
    lane_name = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else ("top" if "_top" in tag else "mid")
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 0
    only = sys.argv[sys.argv.index("--kind") + 1] if "--kind" in sys.argv else None
    t0 = time.time()
    for label, planner in (("old rules", False), ("planner", True)):
        c, picks = run(tag, lane_name, planner)
        died = c.pop("_died")
        tot = sum(v for k, v in c.items() if k != "hold / nothing")
        print(f"{label:10s} orders {tot:4d} | " + "  ".join(f"{k} {v} ({v / max(1, tot):.0%})" for k, v in c.most_common() if k != "hold / nothing")
              + f"  | idle ticks {c['hold / nothing']}")
        kinds = sorted({k for k, _ in died})
        print(" " * 11 + "last-hit targets that died within 1.5 s: "
              + "  ".join(f"{k} {died[(k, True)]}/{died[(k, True)] + died[(k, False)]}" for k in kinds))
        if planner and show:
            picks = [pk for pk in picks if only is None or pk[1]["pick"]["kind"] == only]
            for f, pl in picks[:: max(1, len(picks) // show)][:show]:
                print("   ", f, pl["pick"], "| also", [(o["kind"], o["value"]) for o in pl["also"]])
    print(f"({time.time() - t0:.0f} s)")
