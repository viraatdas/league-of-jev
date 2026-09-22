"""Jev's tactical head: the fast decision, asked back-to-back (~7 per second) while in lane.

Shaped like OpenAI Five's action space (arXiv 1912.06680, Appendix F): a primary action chosen
from a filtered list of what is available right now, plus target parameters that are read only
when the chosen action needs them. Jev answers all questions of a pack in parallel, so each
targeted spell gets its own target question (Five conditioned one unit head on the action;
parallel questions cannot, so the heads are split per spell instead).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

from jev import config
from jev.micro import Scene, Track

load_dotenv()
VC = config.VISION

ACTIONS: dict[str, str] = {
    "farm": "Keep farming: last-hit minions that are about to die, otherwise hold just behind the wave.",
    "push": "Shove the wave: attack minions freely, ignore last-hit timing.",
    "q_minions": "Q into the minions: last-hits what Q can kill and builds Q stacks toward the tornado.",
    "poke_q": "Q the enemy champion: a quick poke that also builds a Q stack.",
    "tornado": "Throw the Q3 tornado at the enemy champion: knocks them up and enables R.",
    "eq_champion": "E through the enemy champion and Q during the dash (circle Q): an all-in opener.",
    "gapclose": "E through a minion toward the enemy champion (Q during the dash if ready) to get in range.",
    "auto_champion": "Auto-attack the enemy champion.",
    "ult": "R (Last Breath) on the airborne enemy champion: big damage, only possible while they are knocked up.",
    "wind_wall": "W wind wall toward the enemy champion to block their projectiles and skillshots.",
    "back_off": "Walk back toward my tower, away from the enemy champion.",
}


def _dist_label(d: float | None) -> str:
    return "not visible" if d is None else f"{int(round(d / 25.0) * 25)} units"


def available(sc: Scene, q3: bool) -> list[str]:
    """Action filters: only what can be executed now (like Five's per-step availability mask)."""
    acts = ["farm", "back_off"]
    if sc.minions:
        acts.append("push")
        if sc.ready.get("Q"):
            acts.append("q_minions")
    c, d = sc.champ, sc.champ_dist
    if c is not None and d is not None:
        if sc.ready.get("Q") and not q3 and d <= VC.q_range:
            acts.append("poke_q")
        if sc.ready.get("Q") and q3 and d <= VC.q3_range:
            acts.append("tornado")
        if sc.ready.get("E") and d <= VC.e_range and c.e_marked_until < time.time():
            acts.append("eq_champion")
        if sc.ready.get("E") and sc.dash_toward is not None:
            acts.append("gapclose")
        if d <= VC.auto_range + 150:
            acts.append("auto_champion")
        if sc.r_lit and d <= VC.r_range:
            acts.append("ult")
        if sc.ready.get("W") and d <= 1100:
            acts.append("wind_wall")
    return acts


def tactical_state(sc: Scene, q3: bool, api: dict, plan: dict) -> dict:
    me = api.get("me", {})
    opp = api.get("lane_opponent") or {}
    champ = None
    if sc.champ is not None:
        champ = {
            "champion": opp.get("champion", "enemy"),
            "level": opp.get("level"),
            "hp_percent": int(sc.champ.unit.hp * 100),
            "distance": _dist_label(sc.champ_dist),
            "airborne_now": sc.r_lit,
        }
    return {
        "plan_from_strategy": plan,
        "me": {
            "champion": "Yasuo",
            "level": me.get("level"),
            "hp_percent": me.get("hp_percent"),
            "Q": ("Q3 tornado ready" if q3 else "ready") if sc.ready.get("Q") else "on cooldown",
            "W": "ready" if sc.ready.get("W") else "not ready",
            "E": "ready" if sc.ready.get("E") else "not ready",
            "R": "castable (enemy airborne)" if sc.r_lit else "not castable",
            "flash": "ready" if sc.ready.get("D") else "not ready",
        },
        "enemy_champion": champ,
        "enemy_minions_on_screen": len(sc.minions),
        "minions_killable_by_auto_now": len(sc.killable_auto),
        "minions_killable_by_q_now": len(sc.killable_q),
        "ally_minions_on_screen": sc.allies,
        "danger_from_strategy": plan.get("danger"),
    }


@dataclass
class Tactic:
    seq: int
    action: str
    confidence: float
    probabilities: dict[str, float]
    state_ts: float
    latency_ms: float
    tokens: int = 0
    dash_target: int | None = None
    raw: Any = field(default=None, repr=False)

    def age(self, now: float) -> float:
        return now - self.state_ts


class TacticalBrain:
    """Runs back-to-back Jev calls on the freshest scene. The main loop publishes (scene, q3,
    api_state, plan) with publish(); the latest answer is in .tactic."""

    def __init__(self, max_hz: float = 8.0) -> None:
        self.client = TypeSafeClient(retry=RetryPolicy(max_retries=0, timeout=1.0))
        self.max_hz = max_hz
        self.tactic: Tactic | None = None
        self._input: tuple | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0
        self.calls = 0
        self.errors = 0
        self._rate: list[float] = []
        self.last_error = ""

    def publish(self, sc: Scene, q3: bool, api: dict, plan: dict, ts: float) -> None:
        with self._lock:
            self._input = (sc, q3, api, plan, ts)

    def rate(self) -> float:
        now = time.time()
        self._rate = [t for t in self._rate if now - t < 5.0]
        return len(self._rate) / 5.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        period = 1.0 / self.max_hz
        while not self._stop.is_set():
            t0 = time.time()
            with self._lock:
                inp, self._input = self._input, None
            if inp is None:
                time.sleep(0.03)
                continue
            sc, q3, api, plan, ts = inp
            acts = available(sc, q3)
            if len(acts) <= 2 and not sc.minions and sc.champ is None:
                time.sleep(0.05)
                continue
            questions = {"action": Choice(
                instructions=("You are Yasuo in mid lane. Pick the single best move for the next "
                              "half second, following `plan_from_strategy` unless the situation clearly "
                              "calls for something else."),
                criteria={a: ACTIONS[a] for a in acts},
            )}
            if "gapclose" in acts and len(sc.dash_options) >= 2:
                # Target head, read only if the action is gapclose (Five's unit-selection parameter).
                questions["dash_target"] = Choice(
                    instructions="If Yasuo dashes through a minion to reach the enemy champion, which minion?",
                    criteria={f"minion_{tr.id}": (f"{int(tr.unit.hp * 100)}% HP, {int(sc.dist(tr))} units from me, "
                                                  f"lands {int(g)} units closer to the enemy champion")
                              for tr, g in sc.dash_options},
                )
            try:
                res = self.client.system_one(tactical_state(sc, q3, api, plan), questions)
                ch = res.choices["action"]
                dash = None
                if "dash_target" in questions:
                    try:
                        dash = int(str(res.choices["dash_target"].choice).split("_")[1])
                    except (KeyError, IndexError, ValueError):
                        dash = None
                self._seq += 1
                usage = getattr(res, "usage", None)
                self.tactic = Tactic(
                    seq=self._seq, action=ch.choice, confidence=float(ch.confidence),
                    probabilities={k: round(float(v), 3) for k, v in dict(ch.probabilities).items()},
                    state_ts=ts, latency_ms=(time.time() - t0) * 1000,
                    tokens=int(getattr(usage, "input_tokens", 0) or 0), dash_target=dash, raw=res,
                )
                self.calls += 1
                self._rate.append(time.time())
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:160]
                time.sleep(0.2)
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
