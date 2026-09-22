"""Decision log and review: how we find out whether the action menu is holding play back.

Every tactical decision is written with its menu, Jev's probabilities, the menu_fits answer,
whether it was an exploration sample, and what happened in the next few seconds (own HP, gold,
CS, kills, deaths, the enemy champion's HP from the health bar). `jev review` summarises:
  - outcome per action, and for explored vs top-choice picks of the same action
  - states where Jev said no listed move fits (candidates for new actions)
  - near-ties, where Jev's top two moves were close (candidates for a better target head)
"""
from __future__ import annotations

import collections
import json
import time
from pathlib import Path

HORIZON_S = 3.0


class DecisionLog:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.pending: list[tuple[float, dict, dict]] = []

    def record(self, tactic, executed: bool, metrics: dict, game_time: str) -> None:
        if self.path is None:
            return
        probs = tactic.probabilities
        top2 = sorted(probs.values(), reverse=True)[:2] + [0.0]
        entry = {
            "ts": round(time.time(), 3), "game_time": game_time, "action": tactic.action,
            "executed": executed, "explored": tactic.explored, "confidence": round(tactic.confidence, 3),
            "margin": round(top2[0] - top2[1], 3), "menu_fits": round(tactic.menu_fits, 3),
            "probabilities": probs, "menu": tactic.menu, "extra": tactic.extra,
            "latency_ms": round(tactic.latency_ms), "state": tactic.state,
        }
        self.pending.append((time.time(), entry, dict(metrics)))

    def resolve(self, metrics: dict) -> None:
        """Attach outcomes to decisions older than the horizon and append them to the file."""
        if self.path is None or not self.pending:
            return
        now = time.time()
        done = [p for p in self.pending if now - p[0] >= HORIZON_S]
        if not done:
            return
        self.pending = [p for p in self.pending if now - p[0] < HORIZON_S]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            for _, entry, m0 in done:
                out = {k: round(metrics.get(k, 0) - m0.get(k, 0), 2) for k in ("hp", "gold", "cs", "kills", "deaths", "assists")}
                if m0.get("enemy_hp") is not None and metrics.get("enemy_hp") is not None:
                    out["enemy_hp"] = round(metrics["enemy_hp"] - m0["enemy_hp"], 2)
                entry["outcome"] = out
                entry["score"] = score(out)
                f.write(json.dumps(entry) + "\n")


def score(o: dict) -> float:
    """One number for 'did this go well': kills and assists up, deaths way down, gold and CS up,
    damage dealt to the enemy champion up, own HP lost down."""
    return round(3.0 * o.get("kills", 0) + 1.5 * o.get("assists", 0) - 4.0 * o.get("deaths", 0)
                 + o.get("gold", 0) / 40.0 + 0.5 * o.get("cs", 0)
                 - 3.0 * o.get("enemy_hp", 0)        # enemy HP is a 0..1 fraction; a drop scores
                 + o.get("hp", 0) / 50.0, 3)          # own HP in percent points; a drop costs


def review(path: str | Path, top: int = 8) -> str:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    if not rows:
        return "no decisions logged"
    lines = [f"{len(rows)} decisions from {path}"]
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["action"], r.get("explored", False))].append(r)
    lines.append("\naction            pick     n   mean score  mean dmg to enemy  mean own hp")
    for (a, ex), rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        ms = sum(r["score"] for r in rs) / len(rs)
        de = [r["outcome"].get("enemy_hp") for r in rs if r["outcome"].get("enemy_hp") is not None]
        hp = sum(r["outcome"].get("hp", 0) for r in rs) / len(rs)
        lines.append(f"{a:17} {'explore' if ex else 'top    '} {len(rs):4}   {ms:9.2f}   {(-sum(de) / len(de) * 100) if de else float('nan'):14.1f}%   {hp:9.1f}")
    low = sorted(rows, key=lambda r: r.get("menu_fits", 1.0))[:top]
    lines.append(f"\nmenu gaps (Jev said no listed move fits), lowest {len(low)}:")
    for r in low:
        st = r.get("state", {})
        lines.append(f"  fits={r['menu_fits']:.2f} t={r['game_time']} chose {r['action']} from {r['menu']} "
                     f"| me {st.get('me', {}).get('hp_percent')}% enemy {st.get('enemy_champion')}")
    ties = [r for r in rows if r.get("margin", 1) < 0.08]
    lines.append(f"\nnear-ties (top two within 0.08): {len(ties)} of {len(rows)} ({100 * len(ties) / len(rows):.0f}%)")
    pairs = collections.Counter(tuple(sorted(sorted(r["probabilities"], key=lambda k: -r["probabilities"][k])[:2])) for r in ties)
    for (a, b), n in pairs.most_common(5):
        lines.append(f"  {a} vs {b}: {n}")
    return "\n".join(lines)
