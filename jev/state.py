"""Turn raw Riot data plus screen perception into the small JSON state Jev is asked about.

Jev loses accuracy as irrelevant state grows, so this is deliberately compact and every
number that code can compute exactly (timers, diffs) is computed here, not asked of Jev.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

DRAGON_FIRST_SPAWN = 300.0
DRAGON_RESPAWN = 300.0
HERALD_FIRST_SPAWN = 480.0
BARON_FIRST_SPAWN = 1200.0
BARON_RESPAWN = 360.0


@dataclass
class Perception:
    """API-derived situational signals. No screen reading in v0."""

    position: str = "unknown"            # base | traveling | lane | forward | retreating | dead
    lane_progress_pct: int | None = None  # 0 = own fountain, 100 = enemy fountain (dead reckoned)
    hp_lost_recent_pct: float = 0.0       # HP% lost in the last few seconds
    seconds_since_damage: float | None = None
    recalling: bool = False

    def to_state(self) -> dict[str, Any]:
        if self.hp_lost_recent_pct >= 12:
            damage = "taking heavy damage right now"
        elif self.hp_lost_recent_pct >= 3:
            damage = "taking some damage"
        elif self.seconds_since_damage is not None and self.seconds_since_damage < 8:
            damage = "was hit a few seconds ago"
        else:
            damage = "not being attacked"
        where = self.position
        if self.lane_progress_pct is not None and where in ("lane", "forward", "traveling"):
            if self.lane_progress_pct >= 56:
                where = "pushed up near the enemy tower"
            elif self.lane_progress_pct >= 47:
                where = "at the middle of mid lane"
            elif self.lane_progress_pct >= 38:
                where = "near my own mid tower"
            else:
                where = "walking from base to lane"
        return {"where_i_am": where, "damage": damage, "recalling": self.recalling}


def _name(p: dict) -> str:
    return p.get("riotId") or p.get("riotIdGameName") or p.get("summonerName") or ""


def find_me(data: dict) -> dict | None:
    ap = data.get("activePlayer", {})
    me_name = ap.get("riotId") or ap.get("summonerName") or ""
    for p in data.get("allPlayers", []):
        n = _name(p)
        if n == me_name or (me_name and n.split("#")[0] == me_name.split("#")[0]):
            return p
    return None


def _items(p: dict) -> list[str]:
    return [it.get("displayName", "?") for it in p.get("items", []) if it.get("displayName")]


def _player_brief(p: dict) -> dict:
    s = p.get("scores", {})
    return {
        "champion": p.get("championName"),
        "level": p.get("level"),
        "items": _items(p),
        "kda": f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
        "cs": s.get("creepScore", 0),
        "alive": not p.get("isDead", False),
        "respawn_in_s": round(p.get("respawnTimer", 0.0)) if p.get("isDead") else 0,
    }


def _event_line(e: dict) -> str:
    t = e.get("EventTime", 0.0)
    mm, ss = int(t // 60), int(t % 60)
    name = e.get("EventName", "?")
    if name == "ChampionKill":
        return f"{mm}:{ss:02d} {e.get('KillerName')} killed {e.get('VictimName')}"
    if name in ("DragonKill", "BaronKill", "HeraldKill"):
        return f"{mm}:{ss:02d} {name} by {e.get('KillerName')} ({e.get('DragonType', '')})".rstrip("( )")
    if name == "TurretKilled":
        return f"{mm}:{ss:02d} turret {e.get('TurretKilled')} destroyed by {e.get('KillerName')}"
    return f"{mm}:{ss:02d} {name}"


def objective_timers(events: list[dict], now: float) -> dict[str, float | None]:
    last_dragon = max((e["EventTime"] for e in events if e.get("EventName") == "DragonKill"), default=None)
    last_baron = max((e["EventTime"] for e in events if e.get("EventName") == "BaronKill"), default=None)
    herald_taken = any(e.get("EventName") == "HeraldKill" for e in events)
    next_dragon = DRAGON_FIRST_SPAWN if last_dragon is None else last_dragon + DRAGON_RESPAWN
    next_baron = BARON_FIRST_SPAWN if last_baron is None else last_baron + BARON_RESPAWN
    return {
        "next_dragon_in_s": max(0, round(next_dragon - now)),
        "next_baron_in_s": max(0, round(next_baron - now)) if now >= 0 else None,
        "herald_available": (not herald_taken) and HERALD_FIRST_SPAWN <= now < BARON_FIRST_SPAWN,
    }


def build_state(data: dict, perception: Perception | None = None, my_role: str = "MIDDLE") -> dict:
    perception = perception or Perception()
    ap = data.get("activePlayer", {})
    me = find_me(data) or {}
    players = data.get("allPlayers", [])
    my_team = me.get("team", "ORDER")
    allies = [p for p in players if p.get("team") == my_team]
    enemies = [p for p in players if p.get("team") != my_team]
    events = data.get("events", {}).get("Events", [])
    now = float(data.get("gameData", {}).get("gameTime", 0.0))

    cs = ap.get("championStats", {})
    hp, hp_max = cs.get("currentHealth", 0.0), max(cs.get("maxHealth", 1.0), 1.0)
    abilities = ap.get("abilities", {})
    ability_levels = {k: abilities.get(k, {}).get("abilityLevel", 0) for k in ("Q", "W", "E", "R")}

    opp = next((p for p in enemies if p.get("position") == my_role), None)
    if opp is None and enemies:
        opp = max(enemies, key=lambda p: p.get("level", 0))  # best guess without lane info

    def team_stats(ps: list[dict]) -> dict:
        return {
            "kills": sum(p.get("scores", {}).get("kills", 0) for p in ps),
            "deaths": sum(p.get("scores", {}).get("deaths", 0) for p in ps),
            "alive": sum(1 for p in ps if not p.get("isDead")),
            "dead": [p.get("championName") for p in ps if p.get("isDead")],
            "total_levels": sum(p.get("level", 0) for p in ps),
        }

    recent = [_event_line(e) for e in events if now - e.get("EventTime", 0) <= 60 and e.get("EventName") != "GameStart"]
    mm, ss = int(now // 60), int(now % 60)
    return {
        "game": {"time": f"{mm}:{ss:02d}", "minutes": round(now / 60, 1), "mode": data.get("gameData", {}).get("gameMode")},
        "me": {
            "champion": me.get("championName", "Yasuo"),
            "role": my_role,
            "level": ap.get("level", me.get("level")),
            "hp_percent": round(100 * hp / hp_max),
            "gold": round(ap.get("currentGold", 0)),
            "items": _items(me),
            "ability_levels": ability_levels,
            "kda": _player_brief(me)["kda"] if me else "0/0/0",
            "cs": me.get("scores", {}).get("creepScore", 0),
            "alive": not me.get("isDead", False),
        },
        "lane_opponent": _player_brief(opp) if opp else None,
        "my_team": team_stats(allies),
        "enemy_team": team_stats(enemies),
        "nearby": perception.to_state(),
        "objectives": objective_timers(events, now),
        "recent_events": recent[-8:],
    }
