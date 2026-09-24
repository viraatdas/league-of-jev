"""Jev's fight head: a dedicated call, asked back to back while an enemy champion is on screen or
close on the minimap, that reads the fight and plans the next few seconds.

The tactical head sees one frame of text and picked "farm" 80-95% of the time with the enemy in
view; its danger read lagged the deaths it should have prevented (overnight games 4-8). This head
gets what a player weighs in a fight: both sides' HP and how it moved over the last one and three
seconds, what is ready on our side, the kill threat, levels and items, minions around each of us,
towers, and who else is coming from the minimap. It answers five typed questions in one call:

  plan          all_in / trade / poke / farm / back_off / escape (a Choice)
  win_all_in    do I kill the focused champion and survive if I commit now (a Noul)
  trade_worth   would a short trade cost them more HP than me (a Noul)
  in_danger     will I die in the next few seconds if I stay (a Noul)
  gank_coming   is someone I cannot see about to arrive (a Noul)
  focus         which enemy champion, when more than one is on screen (a Choice)

The loop turns the answer into the frame-rate combo layer's mode, keeping a few hard floors in
code (two on one with no ally, tower dives), and logs every read with what happened three
seconds later, so the thresholds can be calibrated (scripts/fight_calibration.py).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

load_dotenv()

PLANS: dict[str, str] = {
    "all_in": "Commit to killing the focused enemy champion now: close the gap, full combo, ignite, chase until they die.",
    "trade": "One short exchange: my burst and an auto or two on them, then step back before they answer.",
    "poke": "Hit them only with what reaches from where I stand (a skillshot), without walking or dashing in.",
    "farm": "Ignore them for now: keep last-hitting where I am; no need to walk away.",
    "back_off": "Walk back toward my tower, out of their reach, and wait for a better moment.",
    "escape": "Get out now with everything I have (dash, Flash): staying means dying.",
}


@dataclass
class FightRead:
    seq: int
    ts: float                     # when the features were taken
    latency_ms: float
    plan: str
    plan_probs: dict[str, float]
    win_all_in: float
    trade_worth: float
    in_danger: float
    gank_coming: float
    focus: str | None = None
    focus_probs: dict[str, float] = field(default_factory=dict)
    state: dict = field(default_factory=dict, repr=False)

    def age(self, now: float) -> float:
        return now - self.ts

    def summary(self) -> str:
        return (f"{self.plan} (p={self.plan_probs.get(self.plan, 0):.2f}) win={self.win_all_in:.2f} trade={self.trade_worth:.2f} "
                f"danger={self.in_danger:.2f} gank={self.gank_coming:.2f}" + (f" focus={self.focus}" if self.focus else ""))


def questions(state: dict, role_text: str) -> dict[str, Any]:
    qs: dict[str, Any] = {
        "plan": Choice(
            instructions=(f"You are `me`, {role_text}, and an enemy champion is close. Plan the next three seconds. "
                          "In lane most moments are farm or poke; back off or escape only when they can take more HP than "
                          "I can afford, and trade or all in when my burst clearly wins the exchange. "
                          "Weigh `numbers` first (how many of them and of us are close, how long I last), then HP and how it "
                          "has been moving (`hp_last_1s`, `hp_last_3s`), what I have ready, levels and items, minions around "
                          "each of us, towers, and enemies who may arrive (`map`)."),
            criteria=PLANS,
        ),
        "win_all_in": Noul(instructions=("If I commit to killing the focused enemy champion right now, do I kill them "
                                         "and survive? Count my ready abilities and summoners, both HP totals, levels, "
                                         "minions, towers and anyone else close.")),
        "trade_worth": Noul(instructions="Would a short trade right now take more HP off them than off me?"),
        "in_danger": Noul(instructions=("Will I die in the next few seconds if I stay where I am? `numbers` says how many of "
                                        "them and of us are close and how many seconds I last at the rate I am losing HP "
                                        "now; two of them on me with no ally, or under five seconds to live, is deadly.")),
        "gank_coming": Noul(instructions=("Is an enemy I cannot see on screen (their jungler or a roaming laner) likely to "
                                          "arrive in the next few seconds? Use `map` and the enemies missing from it.")),
    }
    enemies = state.get("enemies_on_screen") or {}
    if len(enemies) >= 2:
        qs["focus"] = Choice(instructions="Which enemy champion on screen should I focus?", criteria={
            k: v.get("describe", k) for k, v in enemies.items()})
    return qs


class FightBrain:
    """Back-to-back fight reads on the freshest features (same shape as TacticalBrain)."""

    def __init__(self, role_text: str, max_hz: float = 6.0, log_path: str | None = None) -> None:
        self.client = TypeSafeClient(retry=RetryPolicy(max_retries=0, timeout=1.2))
        self.role_text = role_text
        self.max_hz = max_hz
        self.read: FightRead | None = None
        self._input: tuple[dict, float] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0
        self.calls = self.errors = 0
        self.last_error = ""
        self._rate: list[float] = []
        self.log = FightLog(log_path)

    def publish(self, state: dict, ts: float) -> None:
        with self._lock:
            self._input = (state, ts)

    def stop(self) -> None:
        self._stop.set()

    def rate(self) -> float:
        now = time.time()
        self._rate = [t for t in self._rate if now - t < 5.0]
        return len(self._rate) / 5.0

    def ask(self, state: dict, ts: float) -> FightRead:
        t0 = time.time()
        qs = questions(state, self.role_text)
        res = self.client.system_one(state, qs)
        plan = res.choices["plan"]
        probs = lambda ch: {k: round(float(v), 3) for k, v in dict(ch.probabilities).items()}
        focus = res.choices["focus"] if "focus" in qs else None
        self._seq += 1
        return FightRead(
            seq=self._seq, ts=ts, latency_ms=(time.time() - t0) * 1000, plan=plan.choice, plan_probs=probs(plan),
            win_all_in=float(res.nouls["win_all_in"].noul), trade_worth=float(res.nouls["trade_worth"].noul),
            in_danger=float(res.nouls["in_danger"].noul), gank_coming=float(res.nouls["gank_coming"].noul),
            focus=focus.choice if focus is not None else None, focus_probs=probs(focus) if focus is not None else {},
            state=state,
        )

    def run(self) -> None:
        period = 1.0 / self.max_hz
        while not self._stop.is_set():
            t0 = time.time()
            with self._lock:
                inp, self._input = self._input, None
            if inp is None:
                time.sleep(0.01)
                continue
            try:
                self.read = self.ask(*inp)
                self.calls += 1
                self._rate.append(time.time())
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:160]
                time.sleep(0.2)
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)


class FightLog:
    """Every applied fight read with what happened three seconds later: my HP change, the focused
    champion's HP change, kills and deaths. The calibration script reads this file."""

    HORIZON_S = 3.0

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.pending: list[tuple[float, dict, dict]] = []
        self._last_seq = 0

    def record(self, read: FightRead, applied: str, m0: dict) -> None:
        if self.path is None or read.seq == self._last_seq:
            return
        self._last_seq = read.seq
        entry = {"ts": round(time.time(), 2), "seq": read.seq, "plan": read.plan, "plan_probs": read.plan_probs,
                 "win_all_in": round(read.win_all_in, 3), "trade_worth": round(read.trade_worth, 3),
                 "in_danger": round(read.in_danger, 3), "gank_coming": round(read.gank_coming, 3), "focus": read.focus,
                 "applied": applied, "latency_ms": round(read.latency_ms), "state": read.state}
        self.pending.append((time.time(), entry, dict(m0)))

    def resolve(self, m: dict) -> None:
        if self.path is None or not self.pending:
            return
        now = time.time()
        done = [p for p in self.pending if now - p[0] >= self.HORIZON_S]
        if not done:
            return
        self.pending = [p for p in self.pending if now - p[0] < self.HORIZON_S]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            for _, e, m0 in done:
                e["outcome"] = {
                    "my_hp": round(m.get("hp", 0) - m0.get("hp", 0), 1),
                    "their_hp": (round(m["enemy_hp"] - m0["enemy_hp"], 3)
                                 if m.get("enemy_hp") is not None and m0.get("enemy_hp") is not None else None),
                    "kills": m.get("kills", 0) - m0.get("kills", 0), "deaths": m.get("deaths", 0) - m0.get("deaths", 0),
                }
                f.write(json.dumps(e) + "\n")
