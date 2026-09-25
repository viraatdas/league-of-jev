"""The macro situation, once a second: who is alive and for how long, which towers stand, how
many of them are missing from the map, and the window that follows from it.

Bot games are won by turning fights into objectives. Yasuo farmed or walked back to lane while two
or three of them were dead (g18-g26: towers fell to our bots, rarely with him), and pushed past the
middle of the lane with three of them unseen (the pushed-up deaths of g19/g26). The situation names
the window in plain words for Jev's strategy head and gives the code the numbers:

    power play   two or more of them dead for 12+ s more, us not fewer: take a tower or dragon
    outnumbered  two or more of us dead: no skirmishes, farm near our towers
    unseen       three or more of them alive and off the minimap: do not push past the middle
"""
from __future__ import annotations

from dataclasses import dataclass, field

from jev import config
from jev.minimap import dist

TOWER_NAMES = ["top outer", "top inner", "top inhibitor", "mid outer", "mid inner", "mid inhibitor",
               "bot outer", "bot inner", "bot inhibitor", "nexus", "nexus"]


@dataclass
class Situation:
    game_s: float = 0.0
    allies_alive: int = 5
    enemies_alive: int = 5
    enemies_dead: list[tuple[str, float]] = field(default_factory=list)   # (champion, respawn in s)
    allies_dead: list[tuple[str, float]] = field(default_factory=list)
    unseen: int = 0                       # alive enemies not on the minimap (approximate: icons merge)
    our_towers: list[bool] = field(default_factory=list)     # standing, by TOWER_NAMES index
    their_towers: list[bool] = field(default_factory=list)
    power_play: bool = False
    power_until: float = 0.0              # game time the window lasts until (their first respawn)
    outnumbered: bool = False
    level_diff: float = 0.0               # our average level minus theirs
    kill_diff: int = 0

    def frontmost_enemy_tower(self, lane_idx: int) -> int | None:
        """Index of their frontmost standing tower in a lane (0 top, 1 mid, 2 bot)."""
        for k in range(3):
            i = lane_idx * 3 + k
            if i < len(self.their_towers) and self.their_towers[i]:
                return i
        return None

    def summary(self) -> dict:
        """For Jev's strategy head: the facts and the window, in words."""
        ours = [TOWER_NAMES[i] for i, up in enumerate(self.our_towers[:9]) if not up]
        theirs = [TOWER_NAMES[i] for i, up in enumerate(self.their_towers[:9]) if not up]
        if self.power_play:
            window = (f"power play: {len(self.enemies_dead)} of them dead for {self.power_until - self.game_s:.0f}+ s "
                      "- take a tower or the dragon with the team now")
        elif self.outnumbered:
            window = f"{len(self.allies_dead)} of us dead - avoid fights, farm near our towers until they are back"
        elif self.unseen >= 3:
            window = f"{self.unseen} of them missing from the map - do not push past the middle of the lane"
        else:
            window = "even - farm, trade when ahead, group for objectives"
        return {
            "alive": f"we have {self.allies_alive}, they have {self.enemies_alive}",
            "enemies_dead": [f"{n} ({s:.0f} s)" for n, s in self.enemies_dead],
            "allies_dead": [f"{n} ({s:.0f} s)" for n, s in self.allies_dead],
            "enemies_missing_from_map": self.unseen,
            "our_towers_lost": ours or "none",
            "their_towers_destroyed": theirs or "none",
            "average_level_difference": round(self.level_diff, 1),
            "kill_difference": self.kill_diff,
            "window": window,
        }


def analyze(data: dict, mm, my_team: str, dead_ours: set[int], dead_theirs: set[int]) -> Situation:
    """`dead_ours` / `dead_theirs`: tower indexes known to be down (minimap icons, events)."""
    players = data.get("allPlayers", [])
    gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
    allies = [p for p in players if p.get("team") == my_team]
    enemies = [p for p in players if p.get("team") and p.get("team") != my_team]
    s = Situation(game_s=gt)
    s.enemies_dead = [(str(p.get("championName")), float(p.get("respawnTimer") or 0.0)) for p in enemies if p.get("isDead")]
    s.allies_dead = [(str(p.get("championName")), float(p.get("respawnTimer") or 0.0)) for p in allies if p.get("isDead")]
    s.enemies_alive = len(enemies) - len(s.enemies_dead)
    s.allies_alive = len(allies) - len(s.allies_dead)
    visible = len(mm.enemy_champions) if mm is not None else 0
    s.unseen = max(0, s.enemies_alive - visible)
    s.our_towers = [i not in dead_ours for i in range(11)]
    s.their_towers = [i not in dead_theirs for i in range(11)]
    lv = lambda ps: sum(float(p.get("level") or 1) for p in ps) / max(1, len(ps))
    s.level_diff = lv(allies) - lv(enemies)
    kills = lambda ps: sum(int((p.get("scores") or {}).get("kills", 0)) for p in ps)
    s.kill_diff = kills(allies) - kills(enemies)
    long_dead = [r for _, r in s.enemies_dead if r >= 12.0]
    s.power_play = len(long_dead) >= 2 and s.allies_alive >= s.enemies_alive   # (a window needs time left in it)
    s.power_until = gt + (min(long_dead) if long_dead else 0.0)
    s.outnumbered = len(s.allies_dead) - len(s.enemies_dead) >= 2
    return s


def form(data: dict, window_s: float = 300.0) -> float:
    """Aggression multiplier from the last five minutes: my deaths take it down (two deaths: play the
    next minutes safe, the 0/3 and 1/8 spirals of g15-g26), my kills and assists nudge it up."""
    ap = data.get("activePlayer") or {}
    me = (ap.get("riotId") or ap.get("summonerName") or "").split("#")[0]
    if not me:
        return 1.0
    gt = float((data.get("gameData") or {}).get("gameTime", 0.0))
    deaths = kills = 0
    for e in (data.get("events") or {}).get("Events", []):
        if e.get("EventName") != "ChampionKill" or gt - float(e.get("EventTime", 0.0)) > window_s:
            continue
        if str(e.get("VictimName", "")).split("#")[0] == me:
            deaths += 1
        elif str(e.get("KillerName", "")).split("#")[0] == me or me in [str(a).split("#")[0] for a in e.get("Assisters", [])]:
            kills += 1
    f = (1.0 if deaths == 0 else 0.8 if deaths == 1 else 0.6) * (1.0 + 0.1 * min(kills, 3))
    return max(0.5, min(1.3, f))


def power_play_target(s: Situation, me_pos, mm, side: str, allies: list) -> tuple[str, tuple[float, float], str] | None:
    """Where a power play goes: the dragon when it is up and near enough, else their frontmost standing
    tower that our minions or champions are at (a tower alone shoots me), nearest to me."""
    from jev import places as map_places

    if me_pos is None:
        return None
    left = s.power_until - s.game_s
    theirs = config.RED_TOWERS if side == "ORDER" else config.BLUE_TOWERS
    best = None
    for lane_idx in range(3):
        i = s.frontmost_enemy_tower(lane_idx)
        if i is None:
            continue
        t = theirs[i]
        d = dist(me_pos, t)
        if d / 380.0 > left + 4.0:
            continue  # the window closes before I get there
        minions = sum(1 for m in (mm.ally_minions if mm is not None else []) if dist(m, t) < 1000)
        friends = sum(1 for a in allies if dist(a, t) < 1500)
        if minions < 2 and friends < 2:
            continue
        score = d - 1500 * friends - 400 * minions
        if best is None or score < best[0]:
            best = (score, t, f"power play: their {TOWER_NAMES[i]} ({minions} of our minions, {friends} of us there)")
    if best is None and any(s.frontmost_enemy_tower(k) is None for k in range(3)):
        # A lane open to their base: the nexus towers, then the nexus (the game ends there).
        structures = config.RED_STRUCTURES if side == "ORDER" else config.BLUE_STRUCTURES
        standing = [theirs[i] for i in (9, 10) if s.their_towers[i]]
        ours_at = lambda q: (sum(1 for m in (mm.ally_minions if mm is not None else []) if dist(m, q) < 1200)
                             + 2 * sum(1 for a in allies if dist(a, q) < 1800))
        t = max(standing, key=lambda q: (ours_at(q), -dist(me_pos, q))) if standing else structures[-1]
        minions = sum(1 for m in (mm.ally_minions if mm is not None else []) if dist(m, t) < 1200)
        friends = sum(1 for a in allies if dist(a, t) < 1800)
        if (minions >= 2 or friends >= 2) and dist(me_pos, t) / 380.0 <= left + 4.0:
            best = (0.0, t, f"power play: their {'nexus tower' if standing else 'nexus'} ({minions} of our minions, {friends} of us there)")
    if best is not None:
        return ("objective", best[1], best[2])
    return None
