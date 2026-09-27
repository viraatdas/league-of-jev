"""Lee Sin's fight code (kits.LeeSin.fight, the real one) against simulated targets.

A small 2D world in map units, stepped every 50 ms. The kit sees it the way it sees the game: a camera
locked on Lee (screen = Lee + offset * px_per_unit), the target as a tracked health bar, the HUD's
ready icons, and the loop's fight context (ranks, AD, energy, the target's HP pool and armor). Its
orders go through the real Micro to a fake controller that turns keys and clicks back into game
actions with the wiki's numbers (patch 26.19): Sonic Wave travels (1800 u/s, 1200 range, first unit
hit), Q2 dashes and hits harder the lower she is, E1 then E2 slows, R kicks her 700 units, W dashes to
a unit or a ward, autos with Flurry, 200 energy regenerating 10/s. Targets stand, walk away, zig-zag,
walk in, or trade back; every one runs for her tower once under 40%.

It measures what the fight code does, not the game: kill rate, time to kill, Sonic Wave accuracy and
energy starvation per behaviour, over fixed seeds.
Run: uv run python scripts/sim_lee.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import time

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

from jev import config, keybinds  # noqa: E402
from jev.kits import LeeSin  # noqa: E402
from jev.micro import Micro, Scene, Track  # noqa: E402
from jev.vision import Unit  # noqa: E402

VC = config.VISION
PPU = VC.px_per_unit
ME = (862.0, 490.0)
DT = 0.05
HER_RADIUS, LEE_RADIUS = 65.0, 65.0
AUTO_REACH = 125.0 + HER_RADIUS + LEE_RADIUS


class FakeScreen:
    px_w, scale = 1728, 1.0

    def to_points(self, x, y):
        return x, y

    def to_pixels(self, x, y):
        return x, y


class Sim:
    """One fight: Lee at the origin, her `dist` units away along +x; her tower 2,600 units past her."""

    def __init__(self, behavior: str, seed: int, her_hp: float = 0.8, dist: float = 800.0, level: int = 6,
                 ally_minion: bool = False, her_dps: float = 70.0, her_max: float = 1100.0, armor: float = 45.0,
                 flash: bool = True, dodge: float = 0.35) -> None:
        self.rng = random.Random(seed)
        self.behavior = behavior
        self.t0 = time.time()
        self.now = self.t0
        self.lee = [0.0, 0.0]
        self.her = [dist, 0.0]
        self.tower_x = dist + 2600.0
        self.her_max, self.her_hp = her_max, her_max * her_hp
        self.lee_hp = self.lee_max = 1100.0
        self.her_dps, self.her_flash, self.dodge = her_dps, flash, dodge
        self.sidestep_until, self.sidestep_dir = 0.0, 1.0
        self.energy = 200.0
        self.ranks = {"Q": 3, "W": 2, "E": 2, "R": 1} if level >= 6 else {"Q": 2, "W": 1, "E": 1, "R": 0}
        self.level, self.ad, self.bonus_ad, self.armor = level, 95.0, 25.0, armor
        self.ready_at = {k: self.t0 for k in "QWERDF"}
        self.q_mark_until = 0.0     # Q2 window after a Sonic Wave hit
        self.q_used2 = False
        self.e2_until = 0.0
        self.slow_until = 0.0
        self.air_until = 0.0        # kicked: flying, no control
        self.kick_to = None
        self.missile = None         # (x, y, dx, dy, travelled, cast_done_at)
        self.dash = None            # (target point or "her", speed, then)
        self.goal = None            # Lee's move goal (world), or "her" to attack
        self.lee_locked_until = 0.0 # casting
        self.last_auto = 0.0
        self.flurry = 0
        self.ward = None
        self.allies = [[dist - 120.0, 150.0]] if ally_minion else []
        self.her_vel = [0.0, 0.0]
        self.zig_t, self.zig_dir = 0.0, 1.0
        self.q_thrown = self.q_hit = self.starved = 0
        self.log: list[str] = []
        self.kb = keybinds.load()
        self.track = Track(id=1, unit=self._her_unit(), seen=self.now)
        self.mi = Micro(FakeCtl(self), FakeScreen(), self.kb, "ORDER")
        self.mi.summoners = ["flash", "ignite"]
        self.mi.hp_pct = 100.0
        self.mi.ad, self.mi.aspd = self.ad, 0.75
        self.mi.set_mode("all_in", self.now)
        self.kit = LeeSin("JUNGLE")

    # -- the camera -------------------------------------------------------------------------------
    def to_screen(self, p) -> tuple[float, float]:
        return ME[0] + (p[0] - self.lee[0]) * PPU, ME[1] - (p[1] - self.lee[1]) * PPU

    def to_world(self, x, y) -> tuple[float, float]:
        return self.lee[0] + (x - ME[0]) / PPU, self.lee[1] - (y - ME[1]) / PPU

    def dist_her(self) -> float:
        return math.hypot(self.her[0] - self.lee[0], self.her[1] - self.lee[1])

    def _her_unit(self) -> Unit:
        # (vision reads the bar, above the model; kits aim at ground(), champ_ground_dy lower)
        x, y = self.to_screen(self.her)
        y -= config.VISION.champ_ground_dy
        return Unit("champion", "enemy", x, y, max(0.0, self.her_hp / self.her_max), (int(x) - 50, int(y) - 80, 100, 10))

    # -- rules ------------------------------------------------------------------------------------
    def spend(self, cost: float) -> bool:
        if self.energy < cost:
            self.starved += 1
            return False
        self.energy -= cost
        return True

    def hurt(self, dmg: float, what: str) -> None:
        self.her_hp -= dmg
        self.log.append(f"{self.now - self.t0:5.2f} {what} {dmg:.0f} -> {max(0, self.her_hp):.0f}")

    def phys(self, raw: float) -> float:
        return raw * 100.0 / (100.0 + self.armor)

    def ready(self) -> dict:
        r = {k: self.now >= t for k, t in self.ready_at.items()}
        r["Q"] = (r["Q"] and self.missile is None) or (self.now < self.q_mark_until and not self.q_used2)
        r["E"] = r["E"] or self.now < self.e2_until
        if not self.ranks.get("R"):
            r["R"] = False
        return r

    def cast(self, key: str, world: tuple[float, float] | None) -> None:
        now = self.now
        if key == "Q":
            if now < self.q_mark_until and not self.q_used2:
                if self.dist_her() <= 1250 and self.spend(25):
                    self.q_used2 = True
                    self.dash = ("her", 1350.0 + 345.0, "q2")
                return
            if now < self.ready_at["Q"] or self.missile is not None or world is None or not self.spend(50):
                return
            dx, dy = world[0] - self.lee[0], world[1] - self.lee[1]
            n = math.hypot(dx, dy) or 1.0
            self.missile = [self.lee[0], self.lee[1], dx / n, dy / n, 0.0, now + 0.25]
            if self.dist_her() > 600 and self.rng.random() < self.dodge:
                self.sidestep_until, self.sidestep_dir = now + 0.6, self.rng.choice((-1.0, 1.0))
            self.ready_at["Q"] = now + (10, 9, 8, 7, 6)[self.ranks["Q"] - 1]
            self.lee_locked_until = now + 0.25
            self.q_thrown += 1
        elif key == "E":
            if now < self.e2_until:
                if self.dist_her() <= 550 and self.spend(25):
                    self.slow_until = now + 2.5
                    self.e2_until = 0.0
                    self.log.append(f"{now - self.t0:5.2f} E2 slow")
                return
            if now < self.ready_at["E"] or not self.spend(50):
                return
            self.ready_at["E"] = now + 8.0
            self.lee_locked_until = now + 0.25
            self.flurry = 2
            if self.dist_her() <= 450:
                self.hurt((35, 60, 85, 110, 135)[self.ranks["E"] - 1] + self.ad, "E1")
                self.e2_until = now + 3.0
        elif key == "R":
            if now < self.ready_at["R"] or world is None or not self.ranks.get("R"):
                return
            if math.hypot(world[0] - self.her[0], world[1] - self.her[1]) > 150 or self.dist_her() > 375 + 65:
                return
            self.ready_at["R"] = now + 110.0
            self.hurt(self.phys((175, 400, 625)[self.ranks["R"] - 1] + 2 * self.bonus_ad), "R")
            dx, dy = self.her[0] - self.lee[0], self.her[1] - self.lee[1]
            n = math.hypot(dx, dy) or 1.0
            self.kick_to = (self.her[0] + dx / n * 700, self.her[1] + dy / n * 700)
            self.air_until = now + 0.8
            self.flurry = 2
        elif key == "W":
            if now < self.ready_at["W"] or world is None:
                return
            spots = self.allies + ([self.ward] if self.ward else [])
            near = [p for p in spots if math.hypot(p[0] - world[0], p[1] - world[1]) <= 120
                    and math.hypot(p[0] - self.lee[0], p[1] - self.lee[1]) <= 700]
            if near and self.spend(50):
                self.ready_at["W"] = now + 7.0
                self.dash = (tuple(near[0]), 1800.0, "w")
                self.flurry = 2
        elif key == "ward" and world is not None:
            if math.hypot(world[0] - self.lee[0], world[1] - self.lee[1]) <= 625:
                self.ward = list(world)
        elif key == "D" and world is not None and now >= self.ready_at["D"]:     # Flash: 400 units toward the cursor
            dx, dy = world[0] - self.lee[0], world[1] - self.lee[1]
            n = math.hypot(dx, dy) or 1.0
            k = min(400.0, n) / n
            self.lee[0] += dx * k
            self.lee[1] += dy * k
            self.ready_at["D"] = now + 300.0
            self.log.append(f"{now - self.t0:5.2f} Flash")
        elif key == "F" and now >= self.ready_at["F"] and self.dist_her() <= 600:  # Ignite: 50 + 20/level true, over 5 s
            self.ready_at["F"] = now + 180.0
            self.ignite = (now, 50.0 + 20.0 * self.level)
            self.log.append(f"{now - self.t0:5.2f} Ignite")

    def attack(self, world) -> None:
        if math.hypot(world[0] - self.her[0], world[1] - self.her[1]) <= 150:
            self.goal = "her"
        else:
            self.goal = tuple(world)

    # -- the world steps ----------------------------------------------------------------------------
    def step(self) -> None:
        now, dt = self.now, DT
        self.energy = min(200.0, self.energy + 10.0 * dt)
        ig = getattr(self, "ignite", None)
        if ig is not None and now - ig[0] < 5.0:
            self.her_hp -= ig[1] / 5.0 * dt
        # Sonic Wave in flight
        if self.missile is not None and now >= self.missile[5]:
            m = self.missile
            step = 1800.0 * dt
            for k in range(5):
                m[0] += m[2] * step / 5
                m[1] += m[3] * step / 5
                m[4] += step / 5
                if math.hypot(m[0] - self.her[0], m[1] - self.her[1]) <= 60 + HER_RADIUS:
                    self.hurt(self.phys((60, 90, 120, 150, 180)[self.ranks["Q"] - 1] + 0.9 * self.bonus_ad), "Q1")
                    self.q_hit += 1
                    self.q_mark_until, self.q_used2 = now + 3.0, False
                    self.flurry = 2
                    self.missile = None
                    break
                if m[4] >= 1200:
                    self.missile = None
                    break
        # Lee's dash, move, or autos
        if self.dash is not None:
            target, speed, what = self.dash
            tp = self.her if target == "her" else target
            dx, dy = tp[0] - self.lee[0], tp[1] - self.lee[1]
            d = math.hypot(dx, dy)
            stop = HER_RADIUS + LEE_RADIUS if target == "her" else 0.0
            if d - stop <= speed * dt:
                if d > 0:
                    k = max(0.0, (d - stop) / d)
                    self.lee[0] += dx * k
                    self.lee[1] += dy * k
                if what == "q2":
                    missing = 1.0 - max(0.0, self.her_hp) / self.her_max
                    self.hurt(self.phys(((60, 90, 120, 150, 180)[self.ranks["Q"] - 1] + 0.9 * self.bonus_ad) * (1 + missing)), "Q2")
                    self.flurry = 2
                self.dash = None
            else:
                self.lee[0] += dx / d * speed * dt
                self.lee[1] += dy / d * speed * dt
        elif now >= self.lee_locked_until and self.goal is not None:
            tp = self.her if self.goal == "her" else self.goal
            dx, dy = tp[0] - self.lee[0], tp[1] - self.lee[1]
            d = math.hypot(dx, dy)
            if self.goal == "her" and d <= AUTO_REACH:
                aspd = 0.75 * (1.4 if self.flurry > 0 else 1.0)
                if now - self.last_auto >= 1.0 / aspd:
                    self.last_auto = now
                    self.hurt(self.phys(self.ad), "auto")
                    if self.flurry > 0:
                        self.energy = min(200.0, self.energy + (20.0 if self.flurry == 2 else 10.0))
                        self.flurry -= 1
            elif d > 5:
                s = min(d, 345.0 * dt)
                self.lee[0] += dx / d * s
                self.lee[1] += dy / d * s
        # her
        if now < self.air_until and self.kick_to is not None:
            k = dt / max(dt, self.air_until - now + dt)
            self.her[0] += (self.kick_to[0] - self.her[0]) * k
            self.her[1] += (self.kick_to[1] - self.her[1]) * k
            self.her_vel = [0.0, 0.0]
        else:
            speed = 340.0 * (0.55 if now < self.slow_until else 1.0)
            vx, vy = self._her_intent(now)
            if now < self.sidestep_until:
                vx, vy = 0.2 * (vx or 0.0), self.sidestep_dir
            # she hits back in reach; Flash away once, under 30%
            if self.dist_her() <= AUTO_REACH + 40:
                self.lee_hp -= self.her_dps * dt
            if self.her_flash and self.her_hp < 0.3 * self.her_max and self.dist_her() < 500:
                self.her_flash = False
                self.her[0] += 400.0
                self.log.append(f"{now - self.t0:5.2f} she Flashes away")
            n = math.hypot(vx, vy)
            if n > 0:
                vx, vy = vx / n * speed, vy / n * speed
            self.her_vel = [vx, vy]
            self.her[0] += vx * dt
            self.her[1] += vy * dt
        self.now += dt

    def _her_intent(self, now: float) -> tuple[float, float]:
        if self.her_hp < 0.4 * self.her_max:
            return 1.0, 0.0            # home to her tower
        b = self.behavior
        if b == "stand":
            return 0.0, 0.0
        if b == "walk_away":
            return 1.0, 0.0
        if b == "approach":
            return (-1.0, 0.0) if now - self.t0 < 1.5 else (1.0, 0.0)
        if b == "zigzag":
            if now >= self.zig_t:
                self.zig_t = now + self.rng.uniform(0.35, 0.8)
                self.zig_dir = -self.zig_dir
            return 0.6, self.zig_dir
        if b == "trade":
            d = self.dist_her()
            if d <= AUTO_REACH:
                return 0.0, 0.0
            return -(self.her[0] - self.lee[0]), -(self.her[1] - self.lee[1])
        return 0.0, 0.0

    # -- one fight ------------------------------------------------------------------------------------
    def run(self, max_s: float = 12.0) -> dict:
        while self.now - self.t0 < max_s:
            if self.her_hp <= 0:
                return self.result(True)
            if self.lee_hp <= 0:
                return self.result(False, "Lee died")
            self.mi.hp_pct = 100.0 * self.lee_hp / self.lee_max
            if self.her[0] >= self.tower_x:
                return self.result(False, "reached her tower")
            u = self._her_unit()
            tr = self.track
            tr.unit, tr.seen = u, self.now
            tr.hist.append((self.now, u.hp))
            tr.path.append((self.now, u.x, u.y))
            sc = Scene(me_xy=ME, minions=[], move_speed=345.0)
            sc.champ, sc.champ_dist, sc.enemy_champs = tr, self.dist_her(), 1
            sc.ready = self.ready()
            sc.ally_units = [Unit("minion", "ally", *self.to_screen(a), 0.8, (0, 0, 60, 4)) for a in self.allies]
            sc.ctx = {"ranks": dict(self.ranks), "level": self.level, "ad": self.ad, "bonus_ad": self.bonus_ad,
                      "energy": self.energy, "target_level": self.level, "target_max_hp": self.her_max,
                      "target_armor": self.armor, "target_mr": 35.0, "ward_ready": self.ward is None}
            self.mi.run_due(self.now)
            if not hasattr(self, "go"):
                self.mi.hp_pct = 100.0
                self.go = self.kit.go(sc, self.mi, self.now, 1 if self.allies else 0, True)
            self.kit.fight(self.mi, sc, self.now, 0.75, "all_in")
            self.mi.set_mode("all_in", self.now)   # the loop holds the commitment
            self.step()
        return self.result(False, "time")

    def result(self, killed: bool, why: str = "") -> dict:
        return {"killed": killed, "died": why == "Lee died", "t": self.now - self.t0, "why": why, "q_thrown": self.q_thrown, "q_hit": self.q_hit,
                "starved": self.starved, "log": self.log, "go": getattr(self, "go", None)}


class FakeCtl:
    """The controller Micro drives, turned back into game actions (screen px in, world units out)."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.cursor = ME
        kb = sim.kb
        self.keys = {kb.ability(i).key: k for i, k in zip(range(1, 5), "QWER")}
        self.self_w = kb.self_cast(2).key
        self.ward_key = kb.vision_item.key
        self.summ = {kb.summoner(1).key: "D", kb.summoner(2).key: "F"}

    def keys_ok(self) -> bool:
        return True

    def move(self, x, y):
        self.cursor = (x, y)

    def move_to(self, x, y):
        self.cursor = (x, y)
        self.sim.goal = self.sim.to_world(x, y)

    def click(self, x, y, button="right", hold_ms=40, hover=True):
        self.cursor = (x, y)
        self.sim.attack(self.sim.to_world(x, y))

    def attack_move(self, bind, x, y):
        self.cursor = (x, y)
        self.sim.attack(self.sim.to_world(x, y))

    def cast(self, bind, x, y, quick=None):
        self.cursor = (x, y)
        self._key(bind.key, self.sim.to_world(x, y))

    def press(self, bind, hold_ms=35):
        if bind.key == self.self_w:
            return   # W on myself: a shield, nothing to simulate
        self._key(bind.key, self.sim.to_world(*self.cursor))

    def _key(self, key, world):
        if key == self.ward_key:
            self.sim.cast("ward", world)
        elif key in self.keys:
            self.sim.cast(self.keys[key], world)
        elif key in self.summ:
            self.sim.cast(self.summ[key], world)


CASES = [
    # (label, behavior, her HP, distance, extra)          what go should do
    ("farming bot 80%", "stand", 0.8, 700.0, {}),                                    # go
    ("walking off 80%", "walk_away", 0.8, 700.0, {}),                                # go
    ("hurt 50%, Flash", "stand", 0.5, 900.0, {}),                                    # go
    ("zig-zag 70%", "zigzag", 0.7, 800.0, {}),                                       # go
    ("walks in 90%", "approach", 0.9, 1100.0, {}),                                   # go
    ("trades back 90%, hits hard", "trade", 0.9, 500.0, {"her_dps": 140.0}),        # careful
    ("tank 90% (1600 HP, 90 armor)", "stand", 0.9, 700.0, {"her_max": 1600.0, "armor": 90.0}),  # no
    ("full HP bruiser, hits hard", "trade", 1.0, 600.0, {"her_dps": 160.0, "her_max": 1300.0}),  # no
    ("running 60% + our minion by her", "walk_away", 0.6, 850.0, {"ally_minion": True}),         # go
]


def run_all(seeds: int = 20, verbose: bool = False) -> dict:
    out = {}
    for label, behavior, hp, dist, extra in CASES:
        extra = dict(extra)
        ally = extra.pop("ally_minion", False)
        rs = [Sim(behavior, s, hp, dist, ally_minion=ally, **extra).run() for s in range(seeds)]
        qt, qh = sum(r["q_thrown"] for r in rs), sum(r["q_hit"] for r in rs)
        gos = [r for r in rs if r["go"]]
        tk = sorted(r["t"] for r in gos if r["killed"])
        o = out[label] = {"go": len(gos) / len(rs),
                          "kill_when_go": (sum(r["killed"] for r in gos) / len(gos)) if gos else None,
                          "died_when_go": (sum(r["died"] for r in gos) / len(gos)) if gos else None,
                          "kill_forced": sum(r["killed"] for r in rs) / len(rs),
                          "t_med": tk[len(tk) // 2] if tk else None, "q_acc": qh / qt if qt else None,
                          "starved": sum(r["starved"] for r in rs) / len(rs)}
        pct = lambda v: "-" if v is None else f"{v:.0%}"
        print(f"{label:34s} go {pct(o['go']):>4s}  kill|go {pct(o['kill_when_go']):>4s}  died|go {pct(o['died_when_go']):>4s}  "
              f"kill if forced {pct(o['kill_forced']):>4s}  t {'-' if o['t_med'] is None else round(o['t_med'], 1)} s  "
              f"Q {qh}/{qt} ({pct(o['q_acc'])})")
        if verbose:
            for line in rs[0]["log"][:16]:
                print("      " + line)
    return out


if __name__ == "__main__":
    run_all(verbose="-v" in sys.argv)
