"""Jev's tactical head: the fast decision, asked back-to-back (~7 per second) while in lane.

Shaped like OpenAI Five's action space (arXiv 1912.06680, Appendix F): a primary action chosen
from a list filtered to what is possible right now (the champion kit supplies the list and the
filters), plus target heads read only when the chosen action needs them. Jev answers all
questions of a pack in parallel, so each targeted spell gets its own target question.

A `menu_fits` question rides along in the same call: Jev's probability that one of the listed
moves is actually good here. Low values mark states where the menu is missing something; the
decision log keeps them for review so the menu can grow where it matters.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

from jev import config
from jev.micro import Scene

load_dotenv()
VC = config.VISION


def _dist_label(d: float | None) -> str:
    return "not visible" if d is None else f"{int(round(d / 25.0) * 25)} units"


def tactical_state(sc: Scene, kit, mi, now: float, api: dict, plan: dict) -> dict:
    me = api.get("me", {})
    opp = api.get("lane_opponent") or {}
    champ = None
    if sc.champ is not None:
        champ = {
            "champion": opp.get("champion", "enemy"),
            "level": opp.get("level"),
            "hp_percent": int(sc.champ.unit.hp * 100),
            "distance": _dist_label(sc.champ_dist),
            "airborne_now": sc.r_lit if kit.name == "Yasuo" else None,
        }
    return {
        "plan_from_strategy": plan,
        "me": {"champion": kit.name, "role": "support" if kit.support else "laner",
               "level": me.get("level"), "hp_percent": me.get("hp_percent"), **kit.me_state(sc, mi, now)},
        "enemy_champion": champ,
        "enemy_minions_on_screen": len(sc.minions),
        "minions_killable_by_auto_now": len(sc.killable_auto),
        "ally_minions_on_screen": sc.allies,
        "ally_champions_on_screen": len(sc.ally_champs),
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
    extra: dict = field(default_factory=dict)
    menu: list[str] = field(default_factory=list)
    menu_fits: float = 1.0
    state: dict = field(default_factory=dict, repr=False)
    explored: bool = False
    raw: Any = field(default=None, repr=False)

    def age(self, now: float) -> float:
        return now - self.state_ts


class TacticalBrain:
    """Runs back-to-back Jev calls on the freshest scene. The loop publishes (scene, kit, micro,
    api_state, plan, frame_ts) with publish(); the latest answer is in .tactic.

    explore: probability of executing a sample from Jev's distribution instead of its top
    choice (bot games only), so moves Jev rates second-best still get tried and logged."""

    def __init__(self, max_hz: float = 8.0, explore: float = 0.0) -> None:
        self.client = TypeSafeClient(retry=RetryPolicy(max_retries=0, timeout=1.0))
        self.max_hz = max_hz
        self.explore = explore
        self.tactic: Tactic | None = None
        self._input: tuple | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0
        self.calls = 0
        self.errors = 0
        self._rate: list[float] = []
        self.last_error = ""
        self.on_answer = lambda: None

    def publish(self, sc: Scene, kit, mi, api: dict, plan: dict, ts: float) -> None:
        with self._lock:
            self._input = (sc, kit, mi, api, plan, ts)

    def rate(self) -> float:
        now = time.time()
        self._rate = [t for t in self._rate if now - t < 5.0]
        return len(self._rate) / 5.0

    def stop(self) -> None:
        self._stop.set()

    def ask(self, sc: Scene, kit, mi, api: dict, plan: dict, ts: float) -> Tactic | None:
        t0 = time.time()
        acts = kit.available(sc, mi, ts)
        if len(acts) <= 2 and not sc.minions and sc.champ is None:
            return None
        state = tactical_state(sc, kit, mi, ts, api, plan)
        questions: dict[str, Any] = {
            "action": Choice(
                instructions=(f"You are {kit.role_text(plan.get('lane', 'mid'))}. Pick the single best move for "
                              "the next half second, following `plan_from_strategy` unless the situation clearly "
                              "calls for something else."),
                criteria={a: kit.actions[a] for a in acts},
            ),
            "menu_fits": Noul(instructions=(
                "Is at least one of these moves genuinely good right now? Say false if the right play here is "
                "something none of them covers: " + ", ".join(acts))),
        }
        heads = kit.heads(sc, acts)
        questions.update(heads)
        res = self.client.system_one(state, questions)
        ch = res.choices["action"]
        probs = {k: round(float(v), 3) for k, v in dict(ch.probabilities).items()}
        action, explored = ch.choice, False
        if self.explore and random.random() < self.explore and len(probs) > 1:
            action = random.choices(list(probs), weights=[max(p, 0.02) for p in probs.values()])[0]
            explored = action != ch.choice
        usage = getattr(res, "usage", None)
        self._seq += 1
        return Tactic(
            seq=self._seq, action=action, confidence=float(ch.confidence), probabilities=probs,
            state_ts=ts, latency_ms=(time.time() - t0) * 1000,
            tokens=int(getattr(usage, "input_tokens", 0) or 0), extra=kit.read_heads(res, heads),
            menu=acts, menu_fits=float(res.nouls["menu_fits"].noul), state=state, explored=explored, raw=res,
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
                t = self.ask(*inp)
                if t is None:
                    time.sleep(0.05)
                    continue
                self.tactic = t
                self.on_answer()
                self.calls += 1
                self._rate.append(time.time())
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:160]
                time.sleep(0.2)
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
