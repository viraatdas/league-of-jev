"""Where each enemy champion was last seen, and how soon it could reach a point.

The minimap names the enemy icons (iconid.py); this keeps the last sighting of each. An enemy seen
3,000 units away 2 s ago is at least ~6 s from me walking straight at me; one unseen for 40 s could be
anywhere. The enemy jungler (the one with Smite) matters most: seen on the far side of the map a few
seconds ago, the lane is safe to push and trade in; unseen for a while or close, it is not.
"""
from __future__ import annotations

import math

SPEED = 380.0        # units/s: a champion with boots, a little over most base speeds
FRESH_S = 25.0       # a sighting older than this says nothing about where they are now


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Whereabouts:
    def __init__(self) -> None:
        self.seen: dict[str, tuple[tuple[float, float], float]] = {}   # name -> (map point, time)
        self.jungler: str | None = None

    def update(self, now: float, named: list[tuple[str, tuple[float, float]]]) -> None:
        for name, pt in named:
            self.seen[name] = (pt, now)

    def age(self, name: str, now: float) -> float | None:
        s = self.seen.get(name)
        return None if s is None else now - s[1]

    def eta(self, name: str, pt: tuple[float, float], now: float) -> float | None:
        """Seconds before `name` could stand at `pt`, walking straight from where it was last seen
        (0: could be there already); None when not seen in the last FRESH_S seconds."""
        s = self.seen.get(name)
        if s is None or now - s[1] > FRESH_S:
            return None
        return max(0.0, _dist(s[0], pt) / SPEED - (now - s[1]))

    def jungler_eta(self, pt, now: float) -> float | None:
        return self.eta(self.jungler, pt, now) if self.jungler else None

    def describe(self, me: tuple[float, float] | None, now: float, places=None, alive: set[str] | None = None) -> dict:
        """For Jev's strategy head: each enemy's last sighting in words."""
        out = {}
        for name, (pt, t) in sorted(self.seen.items()):
            if alive is not None and name not in alive:
                continue
            age = now - t
            where = _nearest_place(pt, places) if places else f"{int(pt[0])},{int(pt[1])}"
            label = f"{name} (jungler)" if name == self.jungler else name
            if age > FRESH_S:
                out[label] = f"last seen {where} {age:.0f} s ago: could be anywhere"
            else:
                eta = self.eta(name, me, now) if me is not None else None
                out[label] = (f"seen {where} {age:.0f} s ago"
                              + (f", at least {eta:.0f} s from me" if eta is not None and eta > 0 else ", could be on me now"
                                 if eta is not None else ""))
        return out


def _nearest_place(pt, places: dict) -> str:
    name, (xy, _desc) = min(places.items(), key=lambda kv: _dist(kv[1][0], pt))
    return f"near {name.replace('_', ' ')}"


def jungler_of(all_players: list[dict], my_team: str) -> str | None:
    """The enemy with Smite."""
    for p in all_players or []:
        if p.get("team") == my_team:
            continue
        ss = p.get("summonerSpells") or {}
        raw = " ".join(str((ss.get(k) or {}).get("rawDisplayName", "")) + str((ss.get(k) or {}).get("displayName", ""))
                       for k in ("summonerSpellOne", "summonerSpellTwo"))
        if "smite" in raw.lower():
            return str(p.get("championName"))
    return None
