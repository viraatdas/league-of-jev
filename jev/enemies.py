"""What the enemy champion can do and what she has just used.

Profiles come from Data Dragon (attack range; each basic spell's range and cooldown by rank),
fetched once per champion and cached. Her threat reach is her longest basic spell, not her attack
range: Brand's Q reaches 1050, Nasus's slow 700. When a burst lands on me from her (a drop too big
for her autos, at a distance one of her damaging spells reaches), that spell is counted as used and
on cooldown for its time at her rank: the window to trade back, the one every laner plays around.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jev.items import CACHE, DDRAGON


@dataclass
class Spell:
    key: str
    name: str
    ranges: list[float]
    cooldowns: list[float]
    damaging: bool

    def reach(self) -> float:
        r = max(self.ranges or [0.0])
        return r if 100.0 < r <= 1300.0 else 0.0   # 25000 = global or self-cast

    def cooldown(self, rank: int) -> float:
        if not self.cooldowns:
            return 8.0
        return float(self.cooldowns[max(0, min(rank, len(self.cooldowns)) - 1)])


@dataclass
class Profile:
    name: str
    attack_range: float
    spells: list[Spell] = field(default_factory=list)

    def basics(self) -> list[Spell]:
        return [s for s in self.spells if s.key in ("Q", "W", "E")]

    def reach(self, up: set[str] | None = None) -> float:
        """Her threat reach: attack range or her longest basic spell that is up."""
        r = [self.attack_range]
        r += [s.reach() for s in self.basics() if up is None or s.key in up]
        return max(r)


class EnemyKnowledge:
    def __init__(self, version: str | None = None) -> None:
        self.version = version
        self.profiles: dict[str, Profile | None] = {}
        self.down_until: dict[str, dict[str, float]] = {}   # champion -> spell key -> cooldown end
        self._last_burst = 0.0
        self.events: list[str] = []                         # for the game log

    # -- data ----------------------------------------------------------------------------
    def _version(self) -> str | None:
        if self.version:
            return self.version
        cached = sorted(CACHE.glob("*/champion.json"))
        self.version = cached[-1].parent.name if cached else None
        return self.version

    def profile(self, name: str) -> Profile | None:
        key = (name or "").lower().replace(" ", "").replace("'", "").replace(".", "")
        if not key:
            return None
        if key in self.profiles:
            return self.profiles[key]
        prof = None
        try:
            v = self._version()
            summary = json.loads((CACHE / v / "champion.json").read_text())["data"]
            cid = next(k for k, d in summary.items() if k.lower() == key or d["name"].lower().replace(" ", "") == key)
            f = CACHE / v / "champion" / f"{cid}.json"
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                r = httpx.get(f"{DDRAGON}/cdn/{v}/data/en_US/champion/{cid}.json", timeout=8)
                r.raise_for_status()
                f.write_text(r.text)
            d = json.loads(f.read_text())["data"][cid]
            spells = []
            for k, s in zip("QWER", d.get("spells", [])):
                text = (s.get("tooltip", "") + " " + s.get("description", "")).lower()
                spells.append(Spell(k, s.get("name", k), [float(x) for x in s.get("range", [])],
                                    [float(x) for x in s.get("cooldown", [])], "damage" in text))
            prof = Profile(d["name"], float(d["stats"]["attackrange"]), spells)
        except Exception:  # noqa: BLE001  offline and not cached, or an unknown name
            prof = None
        self.profiles[key] = prof
        return prof

    # -- her cooldowns ---------------------------------------------------------------------
    def up(self, name: str, now: float) -> set[str]:
        down = self.down_until.get(name.lower(), {})
        return {k for k in "QWE" if down.get(k, 0.0) <= now}

    def spells_down(self, name: str, now: float) -> list[str]:
        prof = self.profile(name)
        if prof is None:
            return []
        up = self.up(name, now)
        return [s.key for s in prof.basics() if s.damaging and s.reach() and s.key not in up]

    def reach_now(self, name: str, now: float) -> float | None:
        prof = self.profile(name)
        return None if prof is None else prof.reach(self.up(name, now))

    def burst(self, now: float, name: str, dist_units: float, drop_pct: float, level: int) -> str | None:
        """A drop of `drop_pct` of my HP in the last ~0.4 s with her `dist_units` away: which of her
        damaging spells did it, if one did (None: her autos, or too small to tell)."""
        prof = self.profile(name)
        if prof is None or drop_pct < 6.0 or now - self._last_burst < 1.0:
            return None
        if dist_units <= prof.attack_range + 60 and drop_pct < 9.0:
            return None  # an auto (or two) in her attack range
        up = self.up(name, now)
        cands = [s for s in prof.basics() if s.damaging and s.key in up and s.reach() >= dist_units - 75]
        if not cands:
            return None
        s = min(cands, key=lambda c: c.reach())   # the one whose range fits the distance
        rank = max(1, min(5, (level + 1) // 2))
        self.down_until.setdefault(name.lower(), {})[s.key] = now + 0.9 * s.cooldown(rank)
        self._last_burst = now
        self.events.append(f"{name} {s.key} ({s.name}) used at {dist_units:.0f}u for {drop_pct:.0f}%: down ~{0.9 * s.cooldown(rank):.0f} s")
        return s.key
