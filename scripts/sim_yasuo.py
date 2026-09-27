"""Yasuo's fight code (the real Yasuo.step through Micro) against simulated champions in a lane.

The same idea as sim_lee.py: a 2D world in map units stepped every 50 ms, seen through a camera locked on
Yasuo, with the scene built by the real micro.build_scene (so dash options, the tornado's state and
the combo's inputs are the live ones). Her wave stands between us; the orders come back through a
fake controller as game actions with the wiki's numbers:
  Q      cast 0.35 s (less with attack speed), a 450-unit line 80 wide, hits every unit on it; a hit is a
         stack, two stacks make the next Q the tornado: 1150 units, 180 wide, 1200 u/s, knocks up 1 s
  E      dash through an enemy unit within 475 to 475 units past where I stood (fixed distance), 10 s
         per target; Q in the dash is a 215-unit circle where it lands (a knock-up with the tornado)
  R      blinks to an airborne champion within 1400, keeps her up 1 s more
  autos  AD with a 25% crit chance (x1.75)
Her minions hit me for a while after I hit her; she hits back in reach, sidesteps a long tornado
(it is slow), Flashes away once under 30%, and runs home under 40%.
Run: uv run python scripts/sim_yasuo.py
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
from jev.kits import Yasuo  # noqa: E402
from jev.micro import Micro, Track, build_scene  # noqa: E402
from jev.vision import Hud, Unit, View  # noqa: E402

VC = config.VISION
PPU = VC.px_per_unit
ME = (862.0, 490.0)
DT = 0.05
R_CHAMP, R_MINION = 65.0, 45.0
AUTO_REACH = 175.0 + R_CHAMP


class FakeScreen:
    px_w, scale = 1728, 1.0

    def to_points(self, x, y):
        return x, y

    def to_pixels(self, x, y):
        return x, y


class Minion:
    _n = 0

    def __init__(self, x, y, team, hp=420.0):
        Minion._n += 1
        self.id, self.x, self.y, self.team, self.hp, self.max = Minion._n, x, y, team, hp, hp


class Sim:
    def __init__(self, behavior: str, seed: int, her_hp: float = 0.8, dist: float = 600.0, level: int = 6,
                 her_dps: float = 70.0, her_max: float = 1100.0, armor: float = 45.0, mr: float = 35.0,
                 wave: int = 4, dodge: float = 0.4, flash: bool = True, q3: bool = False) -> None:
        self.rng = random.Random(seed)
        self.behavior = behavior
        self.t0 = self.now = time.time()
        self.me = [0.0, 0.0]
        self.her = [dist, 0.0]
        self.tower_x = dist + 2600.0
        self.her_max, self.her_hp = her_max, her_max * her_hp
        self.hp_max = self.hp = 1050.0
        self.armor, self.mr = armor, mr
        self.her_dps, self.dodge, self.her_flash = her_dps, dodge, flash
        self.level = level
        self.ranks = {"Q": 3, "W": 1, "E": 2, "R": 1} if level >= 6 else {"Q": 3, "W": 1, "E": 1, "R": 0}
        self.ad, self.bonus_ad, self.aspd = 120.0, 45.0, 0.85
        self.ready_at = {k: self.t0 for k in "QWERDF"}
        self.stacks, self.stack_t = (2 if q3 else 0), 0.0
        self.e_marks: dict = {}
        self.dash = None           # (x, y, until, q_in_dash)
        self.cast_lock = 0.0
        self.pending_q = None      # (time, point) a Q in its cast
        self.tornado = None        # [x, y, dx, dy, travelled]
        self.air_until = 0.0
        self.goal = None
        self.last_auto = 0.0
        self.aggro_until = 0.0
        self.sidestep_until, self.sidestep_dir = 0.0, 1.0
        self.zig_t, self.zig_dir = 0.0, 1.0
        self.slow_until = 0.0
        # her wave between us, ours a step in front of me
        self.minions = [Minion(dist * 0.55 + 60 * i, -120 + 80 * i, "enemy") for i in range(wave)]
        self.minions += [Minion(dist * 0.45 - 60 * i, -100 + 70 * i, "ally") for i in range(3)]
        self.q_thrown = self.q_hit_her = self.tornado_thrown = self.tornado_hit = self.r_used = 0
        self.log: list[str] = []
        self.kb = keybinds.load()
        self.her_track = Track(id=1, unit=self._unit_her(), seen=self.now)
        self.min_tracks: dict[int, Track] = {}
        self.mi = Micro(FakeCtl(self), FakeScreen(), self.kb, "ORDER")
        self.mi.summoners = ["flash", "ignite"]
        self.mi.hp_pct = 100.0
        self.mi.ad, self.mi.aspd = self.ad, self.aspd
        self.mi.set_mode("all_in", self.now)
        self.kit = Yasuo()
        self.kit.r_rank = self.ranks.get("R", 0)

    # -- camera --------------------------------------------------------------------------------------
    def to_screen(self, p):
        return ME[0] + (p[0] - self.me[0]) * PPU, ME[1] - (p[1] - self.me[1]) * PPU

    def to_world(self, x, y):
        return self.me[0] + (x - ME[0]) / PPU, self.me[1] - (y - ME[1]) / PPU

    def d(self, a, b) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _unit_her(self) -> Unit:
        x, y = self.to_screen(self.her)
        y -= VC.champ_ground_dy
        return Unit("champion", "enemy", x, y, max(0.0, self.her_hp / self.her_max), (int(x) - 50, int(y) - 80, 100, 10))

    # -- damage -----------------------------------------------------------------------------------
    def phys(self, raw):
        return raw * 100.0 / (100.0 + self.armor)

    def hit_her(self, dmg, what):
        self.her_hp -= dmg
        self.aggro_until = self.now + 2.5
        self.log.append(f"{self.now - self.t0:5.2f} {what} {dmg:.0f} -> {max(0, self.her_hp):.0f}")

    def q_damage(self):
        return (20, 45, 70, 95, 120)[self.ranks["Q"] - 1] + 1.05 * self.ad

    def knock(self, what):
        self.air_until = self.now + 1.0
        self.log.append(f"{self.now - self.t0:5.2f} {what}: she is airborne")

    # -- orders --------------------------------------------------------------------------------------
    def cast(self, key, world):
        now = self.now
        if key == "Q":
            if now < self.ready_at["Q"] or world is None:
                return
            cast_t = max(0.175, 0.35 * (1 - min(0.5, (self.aspd - 0.697) / 0.697 * 0.5)))
            if self.dash is not None and now < self.dash[2]:
                self.dash = (*self.dash[:3], True)          # E+Q: the circle where the dash lands
            else:
                self.pending_q = (now + cast_t, tuple(world))
                self.cast_lock = now + cast_t
            self.ready_at["Q"] = now + 3.5
        elif key == "E":
            if now < self.ready_at["E"] or world is None:
                return
            tgt = self._unit_near(world, 120)
            if tgt is None or self.d(self.me, tgt) > 475 + 30 or now < self.e_marks.get(id(tgt), 0.0):
                return
            self.e_marks[id(tgt)] = now + 10.0
            dx, dy = tgt[0] - self.me[0], tgt[1] - self.me[1]
            n = math.hypot(dx, dy) or 1.0
            self.dash = (self.me[0] + dx / n * 475, self.me[1] + dy / n * 475, now + 0.25, False)
            self.ready_at["E"] = now + 0.4
            dmg = ((70, 85, 100, 115, 130)[self.ranks["E"] - 1] + 0.2 * self.bonus_ad) * 100 / (100 + self.mr)
            if tgt is self.her:
                self.hit_her(dmg, "E")
            else:
                tgt_m = next(m for m in self.minions if [m.x, m.y] == list(tgt))
                tgt_m.hp -= dmg
        elif key == "R":
            if now < self.ready_at["R"] or not self.ranks.get("R") or now >= self.air_until or self.d(self.me, self.her) > 1400:
                return
            self.ready_at["R"] = now + 80.0
            self.me = [self.her[0] - 80.0, self.her[1]]
            self.air_until = now + 1.0
            self.r_used += 1
            self.hit_her(self.phys((200, 350, 500)[self.ranks["R"] - 1] + 1.5 * self.bonus_ad), "R")
        elif key == "D" and world is not None and now >= self.ready_at["D"]:
            dx, dy = world[0] - self.me[0], world[1] - self.me[1]
            n = math.hypot(dx, dy) or 1.0
            k = min(400.0, n) / n
            self.me = [self.me[0] + dx * k, self.me[1] + dy * k]
            self.ready_at["D"] = now + 300.0
            self.log.append(f"{now - self.t0:5.2f} Flash")
        elif key == "F" and now >= self.ready_at["F"] and self.d(self.me, self.her) <= 600:
            self.ready_at["F"] = now + 180.0
            self.ignite = (now, 50.0 + 20.0 * self.level)
            self.log.append(f"{now - self.t0:5.2f} Ignite")

    def _unit_near(self, world, r):
        pts = [self.her] + [[m.x, m.y] for m in self.minions if m.team == "enemy" and m.hp > 0]
        best = min(pts, key=lambda p: self.d(p, world))
        return best if self.d(best, world) <= r else None

    def attack(self, world):
        self.goal = "her" if self.d(world, self.her) <= 150 else tuple(world)

    # -- the world -------------------------------------------------------------------------------------
    def _line_hits(self, a, b, width, units):
        ax, ay = a
        bx, by = b
        L = math.hypot(bx - ax, by - ay) or 1.0
        out = []
        for u, r in units:
            px, py = u[0] - ax, u[1] - ay
            t = (px * (bx - ax) + py * (by - ay)) / L
            if -r <= t <= L + r and abs(px * (by - ay) - py * (bx - ax)) / L <= width / 2 + r:
                out.append(u)
        return out

    def _q_lands(self, point, circle=None):
        q3 = self.stacks >= 2
        units = [(self.her, R_CHAMP)] + [([m.x, m.y], R_MINION) for m in self.minions if m.team == "enemy" and m.hp > 0]
        if circle is not None:
            hit = [u for u, r in units if self.d(u, circle) <= 215 + r]
        elif q3:
            dx, dy = point[0] - self.me[0], point[1] - self.me[1]
            n = math.hypot(dx, dy) or 1.0
            self.tornado = [self.me[0], self.me[1], dx / n, dy / n, 0.0]
            self.tornado_thrown += 1
            self.stacks = 0
            self.kit_log("tornado thrown")
            return
        else:
            dx, dy = point[0] - self.me[0], point[1] - self.me[1]
            n = math.hypot(dx, dy) or 1.0
            end = (self.me[0] + dx / n * 450, self.me[1] + dy / n * 450)
            hit = self._line_hits(self.me, end, 80, units)
        self.q_thrown += 1
        for u in hit:
            if u is self.her:
                self.q_hit_her += 1
                self.hit_her(self.phys(self.q_damage()), "EQ" if circle is not None else "Q")
                if q3:
                    self.knock("EQ3")
            else:
                m = next(m for m in self.minions if [m.x, m.y] == list(u))
                m.hp -= self.phys(self.q_damage())
        if q3:
            self.stacks = 0
        elif hit:
            self.stacks = min(2, self.stacks + 1)
            self.stack_t = self.now

    def kit_log(self, s):
        self.log.append(f"{self.now - self.t0:5.2f} {s}")

    def step(self):
        now, dt = self.now, DT
        ig = getattr(self, "ignite", None)
        if ig is not None and now - ig[0] < 5.0:
            self.her_hp -= ig[1] / 5.0 * dt
        if self.pending_q is not None and now >= self.pending_q[0]:
            self._q_lands(self.pending_q[1])
            self.pending_q = None
        if self.tornado is not None:
            t = self.tornado
            for _ in range(4):
                t[0] += t[2] * 1200 * dt / 4
                t[1] += t[3] * 1200 * dt / 4
                t[4] += 1200 * dt / 4
                if self.d(t, self.her) <= 90 + R_CHAMP:
                    self.tornado_hit += 1
                    self.hit_her(self.phys(self.q_damage()), "tornado")
                    self.knock("tornado")
                    self.tornado = None
                    break
                if t[4] >= 1150:
                    self.tornado = None
                    break
        if self.dash is not None:
            x, y, until, q_in = self.dash
            if now + dt >= until:
                self.me = [x, y]
                if q_in:
                    self._q_lands(None, circle=(x, y))
                self.dash = None
            else:
                k = dt / max(dt, until - now)
                self.me[0] += (x - self.me[0]) * k
                self.me[1] += (y - self.me[1]) * k
        elif now >= self.cast_lock and self.goal is not None:
            tp = self.her if self.goal == "her" else self.goal
            dd = self.d(self.me, tp)
            if self.goal == "her" and dd <= AUTO_REACH:
                if now - self.last_auto >= 1.0 / self.aspd:
                    self.last_auto = now
                    crit = 1.75 if self.rng.random() < 0.25 else 1.0
                    self.hit_her(self.phys(self.ad * crit), "auto")
            elif dd > 5:
                s = min(dd, 345.0 * dt)
                self.me[0] += (tp[0] - self.me[0]) / dd * s
                self.me[1] += (tp[1] - self.me[1]) / dd * s
        # her
        if now >= self.air_until:
            speed = 340.0 * (0.6 if now < self.slow_until else 1.0)
            vx, vy = self._her_intent(now)
            if now < self.sidestep_until:
                vx, vy = 0.2 * vx, self.sidestep_dir
            n = math.hypot(vx, vy)
            if n:
                self.her[0] += vx / n * speed * dt
                self.her[1] += vy / n * speed * dt
            if self.d(self.me, self.her) <= AUTO_REACH + 40:
                self.hp -= self.her_dps * dt
            if self.her_flash and self.her_hp < 0.3 * self.her_max and self.d(self.me, self.her) < 500:
                self.her_flash = False
                self.her[0] += 400.0
                self.kit_log("she Flashes away")
        # a tornado coming from afar: she may step aside
        if self.tornado is not None and self.tornado[4] < 60 and self.d(self.me, self.her) > 700 and self.rng.random() < self.dodge:
            self.sidestep_until, self.sidestep_dir = now + 0.7, self.rng.choice((-1.0, 1.0))
        # her wave hits me for a while after I hit her
        if now < self.aggro_until:
            n_near = sum(1 for m in self.minions if m.team == "enemy" and m.hp > 0 and self.d((m.x, m.y), self.me) <= 550)
            self.hp -= 12.0 * n_near * dt
        self.minions = [m for m in self.minions if m.hp > 0]
        self.now += dt

    def _her_intent(self, now):
        if self.her_hp < 0.4 * self.her_max:
            return 1.0, 0.0
        b = self.behavior
        if b == "stand":
            return 0.0, 0.0
        if b == "walk_away":
            return 1.0, 0.0
        if b == "zigzag":
            if now >= self.zig_t:
                self.zig_t, self.zig_dir = now + self.rng.uniform(0.35, 0.8), -self.zig_dir
            return 0.6, self.zig_dir
        if b == "trade":
            if self.d(self.me, self.her) <= AUTO_REACH:
                return 0.0, 0.0
            return self.me[0] - self.her[0], self.me[1] - self.her[1]
        return 0.0, 0.0

    # -- the kit's view ----------------------------------------------------------------------------
    def view(self) -> View:
        units = [self._unit_her()]
        for m in self.minions:
            x, y = self.to_screen((m.x, m.y))
            units.append(Unit("minion", m.team, x, y, m.hp / m.max, (int(x) - 30, int(y) - 35, int(60 * m.hp / m.max), 4)))
        ready = {k: self.now >= t for k, t in self.ready_at.items()}
        ready["R"] = (ready["R"] and bool(self.ranks.get("R")) and self.now < self.air_until
                      and self.d(self.me, self.her) <= 1400)
        me = Unit("champion", "self", ME[0], ME[1] - VC.champ_ground_dy, self.hp / self.hp_max, (812, 400, 100, 10))
        return View(units=units, me=me, hud=Hud(ready=ready, q3=self.stacks >= 2), ts=self.now)

    def run(self, max_s: float = 12.0) -> dict:
        while self.now - self.t0 < max_s:
            if self.her_hp <= 0:
                return self.result(True)
            if self.hp <= 0:
                return self.result(False, "Yasuo died")
            if self.her[0] >= self.tower_x:
                return self.result(False, "she reached her tower")
            v = self.view()
            her = v.units[0]
            self.her_track.unit, self.her_track.seen = her, self.now
            self.her_track.hist.append((self.now, her.hp))
            self.her_track.path.append((self.now, her.x, her.y))
            mins = []
            for m, u in zip(self.minions, v.units[1:]):
                if m.team != "enemy":
                    continue
                tr = self.min_tracks.get(m.id) or Track(id=1000 + m.id, unit=u, seen=self.now)
                self.min_tracks[m.id] = tr
                tr.unit, tr.seen = u, self.now
                tr.hist.append((self.now, u.hp))
                tr.path.append((self.now, u.x, u.y))
                tr.e_marked_until = max(tr.e_marked_until, 0.0)
                mins.append(tr)
            sc = build_scene(v, mins, [self.her_track], self.ad, self.ranks["Q"], 600.0, self.now, ME,
                             aspd=self.aspd, move_speed=345.0, fwd=(1.0, 0.0), e_dmg=0.0)
            sc.lane = {"opp_range": 175.0, "opp_hp": self.her_max, "aggression": 1.0}
            sc.ctx = {"ranks": dict(self.ranks), "level": self.level, "ad": self.ad, "bonus_ad": self.bonus_ad,
                      "crit": 0.25, "target_max_hp": self.her_max, "target_armor": self.armor, "target_mr": self.mr}
            self.kit.q.hud = self.stacks >= 2
            self.mi.hp_pct = 100.0 * self.hp / self.hp_max
            if not hasattr(self, "go"):
                self.go = self.kit.go(sc, self.mi, self.now, 0, True)
            self.mi.run_due(self.now)
            self.kit.step(self.mi, sc, self.now, self.aspd, "all_in", False)
            self.mi.set_mode("all_in", self.now)
            self.step()
        return self.result(False, "time")

    def result(self, killed, why=""):
        return {"killed": killed, "died": why == "Yasuo died", "t": self.now - self.t0, "why": why, "go": getattr(self, "go", None),
                "q_thrown": self.q_thrown, "q_hit": self.q_hit_her, "tornado": (self.tornado_hit, self.tornado_thrown),
                "r": self.r_used, "hp_left": self.hp / self.hp_max, "log": self.log}


class FakeCtl:
    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.cursor = ME
        kb = sim.kb
        self.keys = {kb.ability(i).key: k for i, k in zip(range(1, 5), "QWER")}
        self.summ = {kb.summoner(1).key: "D", kb.summoner(2).key: "F"}

    def keys_ok(self):
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
        self._key(bind.key, self.sim.to_world(*self.cursor))

    def _key(self, key, world):
        if key in self.keys:
            self.sim.cast(self.keys[key], world)
        elif key in self.summ:
            self.sim.cast(self.summ[key], world)


CASES = [
    ("farming bot 80%", "stand", 0.8, 600.0, {}),
    ("walking off 70%", "walk_away", 0.7, 500.0, {}),
    ("hurt 45%, Flash", "stand", 0.45, 700.0, {}),
    ("zig-zag 70%", "zigzag", 0.7, 650.0, {}),
    ("trades back 80%", "trade", 0.8, 500.0, {"her_dps": 110.0}),
    ("far, tornado range 70%", "stand", 0.7, 1000.0, {}),
    ("no wave (no E targets) 70%", "stand", 0.7, 600.0, {"wave": 0}),
    ("walking off 70%, Q3 ready", "walk_away", 0.7, 500.0, {"q3": True}),
    ("zig-zag 70%, Q3 ready", "zigzag", 0.7, 650.0, {"q3": True}),
    ("far 70%, Q3 ready", "stand", 0.7, 1000.0, {"q3": True}),
    ("full HP bruiser, hits hard", "trade", 1.0, 500.0, {"her_dps": 160.0, "her_max": 1300.0}),
    ("tank 90% (1600 HP, 90 armor)", "stand", 0.9, 600.0, {"her_max": 1600.0, "armor": 90.0}),
]


def run_all(seeds: int = 20, verbose: bool = False) -> dict:
    out = {}
    for label, behavior, hp, dist, extra in CASES:
        rs = [Sim(behavior, s, hp, dist, **extra).run() for s in range(seeds)]
        tk = sorted(r["t"] for r in rs if r["killed"])
        qt, qh = sum(r["q_thrown"] for r in rs), sum(r["q_hit"] for r in rs)
        th, tt = sum(r["tornado"][0] for r in rs), sum(r["tornado"][1] for r in rs)
        gos = [r for r in rs if r["go"]]
        o = out[label] = {"kill": sum(r["killed"] for r in rs) / len(rs), "died": sum(r["died"] for r in rs) / len(rs),
                          "go": len(gos) / len(rs), "kill_when_go": (sum(r["killed"] for r in gos) / len(gos)) if gos else None,
                          "died_when_go": (sum(r["died"] for r in gos) / len(gos)) if gos else None,
                          "t_med": tk[len(tk) // 2] if tk else None, "q_hit": qh / qt if qt else None,
                          "tornado_hit": th / tt if tt else None, "r": sum(r["r"] for r in rs) / len(rs),
                          "hp_left": sum(r["hp_left"] for r in rs) / len(rs)}
        pct = lambda v: "-" if v is None else f"{v:.0%}"
        print(f"{label:30s} go {pct(o['go']):>4s} (kill|go {pct(o['kill_when_go']):>4s})  kill if forced {pct(o['kill']):>4s}  died {pct(o['died']):>4s}  t {'-' if o['t_med'] is None else round(o['t_med'], 1)} s  "
              f"Q on her {qh}/{qt}  tornado {th}/{tt}  R/fight {o['r']:.1f}  HP left {o['hp_left']:.0%}")
        if verbose:
            for line in rs[0]["log"][:18]:
                print("      " + line)
    return out


if __name__ == "__main__":
    run_all(verbose="-v" in sys.argv)
