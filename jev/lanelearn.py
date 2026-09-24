"""Lane learning during the game: the planner's numbers move with what each choice actually did.

Every few seconds of lane play settles something the planner assumed:

    last hits      each attempt's predicted margin (z) and whether the gold came: an online
                   logistic fit per hit kind (auto, Q, E) replaces the fixed kill curve
    her reach      HP lost per second while standing inside her reach, and outside it
    her wave       HP lost in the 2.5 s after I hit her, per minion of hers around her
    trades         her HP lost minus mine in the 3 s after I hit her, per opponent: the trade
                   weight goes up while trades win and down while they lose
    farm dashes    HP lost in the 1.5 s after an E through a minion

The fits start from the last games' values (logs/lane_learned.json) held loosely, so a new
opponent reshapes them within minutes. One summary line a minute goes to the game log.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

SAVE = Path("logs/lane_learned.json")


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


class LaneLearner:
    def __init__(self, opponent: str = "", path: Path = SAVE) -> None:
        self.path = path
        self.opponent = opponent.lower()
        self.cal = {"auto": [1.0, 0.0], "Q": [1.0, 0.0], "E": [1.0, 0.0]}   # p = sig(a * z + b)
        self.cal_n = {"auto": 0, "Q": 0, "E": 0}
        self.cal_hits = {"auto": [0, 0], "Q": [0, 0], "E": [0, 0]}         # paid, tried (this game)
        self.in_reach = 3.0     # % HP per second lost standing in her reach
        self.out_reach = 0.4    # ... and outside it
        self.aggro = 1.5        # % HP lost per minion of her wave after I hit her
        self.edge = {}          # opponent -> her HP% lost minus mine per exchange (average)
        self.edge_n = {}
        self.dash = 1.0         # % HP lost after a farm dash
        self.pending: list[tuple[float, str, dict]] = []
        self._exp: tuple[float, float, bool] | None = None   # (t, hp, inside) of the running window
        self._her_hp: float | None = None
        self.load()

    # -- persistence ------------------------------------------------------------------
    def load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001  first game or unreadable: the priors above
            return
        for k in self.cal:
            if k in d.get("cal", {}):
                self.cal[k] = [float(v) for v in d["cal"][k]]
                self.cal_n[k] = min(20, int(d.get("cal_n", {}).get(k, 0)))  # held loosely: a new game moves them
        self.in_reach = float(d.get("in_reach", self.in_reach))
        self.out_reach = float(d.get("out_reach", self.out_reach))
        self.aggro = float(d.get("aggro", self.aggro))
        self.dash = float(d.get("dash", self.dash))
        self.edge = {k: float(v) for k, v in d.get("edge", {}).items()}
        self.edge_n = {k: min(5, int(v)) for k, v in d.get("edge_n", {}).items()}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "cal": self.cal, "cal_n": self.cal_n, "in_reach": self.in_reach, "out_reach": self.out_reach,
                "aggro": self.aggro, "dash": self.dash, "edge": self.edge, "edge_n": self.edge_n}, indent=1))
        except Exception:  # noqa: BLE001
            pass

    # -- what the planner reads --------------------------------------------------------
    def p_kill(self, kind: str, z: float) -> float:
        a, b = self.cal.get(kind, (1.0, 0.0))
        return _sig(a * z + b)

    def trade_weight(self) -> float:
        """1.0 with no exchanges seen; up to 1.6 while trades win, down to 0.5 while they lose."""
        e = self.edge.get(self.opponent)
        if e is None:
            return 1.0
        return max(0.5, min(1.6, 1.0 + e / 20.0))

    # -- what the loop feeds -------------------------------------------------------------
    def lasthit(self, kind: str, z: float | None, ok: bool) -> None:
        kind = kind.split("-")[0]
        if kind not in self.cal:
            return
        self.cal_hits[kind][0] += int(ok)
        self.cal_hits[kind][1] += 1
        if z is None:
            return
        a, b = self.cal[kind]
        n = self.cal_n[kind]
        lr = 0.25 / math.sqrt(1.0 + n / 10.0)
        g = float(ok) - _sig(a * z + b)
        a = max(0.3, min(3.0, a + lr * g * max(-3.0, min(3.0, z))))
        b = max(-3.0, min(3.0, b + lr * g))
        self.cal[kind] = [a, b]
        self.cal_n[kind] = n + 1

    def exposure(self, now: float, hp: float, inside: bool | None) -> None:
        """HP lost per second in half-second windows, filed under inside / outside her reach
        (windows with no champion in view, or with a gain, are not counted)."""
        if inside is None:
            self._exp = None
            return
        if self._exp is None or self._exp[2] != inside:
            self._exp = (now, hp, inside)
            return
        t0, hp0, _ = self._exp
        if now - t0 < 0.5:
            return
        rate = (hp0 - hp) / (now - t0)
        if rate >= 0:
            if inside:
                self.in_reach += 0.02 * (rate - self.in_reach)
            else:
                self.out_reach += 0.02 * (rate - self.out_reach)
        self._exp = (now, hp, inside)

    def hit_her(self, now: float, my_hp: float, her_hp: float, her_wave: int) -> None:
        if any(k == "trade" and now - d["t"] < 2.0 for _, k, d in self.pending):
            return  # one exchange at a time
        self.pending.append((now + 3.0, "trade", {"t": now, "me": my_hp, "her": her_hp * 100, "wave": her_wave}))

    def dashed(self, now: float, my_hp: float) -> None:
        self.pending.append((now + 1.5, "dash", {"t": now, "me": my_hp}))

    def tick(self, now: float, my_hp: float, her_hp: float | None) -> None:
        if her_hp is not None:
            self._her_hp = her_hp * 100
        keep = []
        for due, kind, d in self.pending:
            if now < due:
                keep.append((due, kind, d))
                continue
            lost = max(0.0, d["me"] - my_hp)
            if kind == "dash":
                self.dash += 0.1 * (lost - self.dash)
            elif kind == "trade":
                base = self.out_reach * (now - d["t"])
                if d["wave"]:
                    self.aggro += 0.1 * (max(0.0, lost - base) / d["wave"] - self.aggro)
                if self._her_hp is not None:
                    edge = (d["her"] - self._her_hp) - lost
                    n = self.edge_n.get(self.opponent, 0)
                    old = self.edge.get(self.opponent, 0.0)
                    self.edge[self.opponent] = old + (edge - old) / min(n + 1, 8)
                    self.edge_n[self.opponent] = n + 1
        self.pending = keep

    def summary(self) -> str:
        cal = "  ".join(f"{k} a={a:.2f} b={b:+.2f} paid {self.cal_hits[k][0]}/{self.cal_hits[k][1]}"
                        for k, (a, b) in self.cal.items())
        e = self.edge.get(self.opponent)
        return (f"learned: {cal} | her reach {self.in_reach:.1f}%/s (outside {self.out_reach:.1f}) | "
                f"her wave {self.aggro:.1f}%/minion | trades vs {self.opponent or '?'} "
                f"{'n/a' if e is None else f'{e:+.0f} HP% ({self.edge_n.get(self.opponent, 0)})'} | dash {self.dash:.1f}%")
