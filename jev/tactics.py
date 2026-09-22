"""Jev's tactical head: the fast decision, asked back-to-back (~7 per second) while in lane.

The action space is jev/actions.py: every move the champion has (from the kit), the universal
moves, both summoner spells and every usable item, plus target / where / distance heads, all in
one parallel call. A `menu_fits` question rides along: Jev's probability that one of the listed
moves is genuinely good here; low values mark gaps for `jev review`.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient

from jev import actions
from jev.actions import Ctx, Spec

load_dotenv()


@dataclass
class TacticInput:
    ctx: Ctx
    api: dict
    plan: dict
    ts: float
    summoners: list[str | None] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    items_ready: dict[int, bool] = field(default_factory=dict)
    hp_pct: float = 100.0
    potion_used_at: float = 0.0
    opp_name: str = "enemy"


def menu(inp: TacticInput) -> list[Spec]:
    ctx = inp.ctx
    ctx.summoners = inp.summoners
    specs = ctx.kit.specs(ctx) + actions.universal(ctx) + actions.summoner_specs(ctx, inp.summoners)
    specs += actions.item_specs(ctx, inp.items, inp.items_ready, inp.hp_pct, inp.potion_used_at)
    seen, out = set(), []
    for s in specs:
        if s.name not in seen:
            seen.add(s.name)
            out.append(s)
    return out


def tactical_state(inp: TacticInput, cands: dict) -> dict:
    ctx = inp.ctx
    sc, kit = ctx.sc, ctx.kit
    me = inp.api.get("me", {})
    fx, fy = ctx.lane.screen_dir(ctx.lane_progress) if ctx.lane is not None else ctx.mi.fwd
    return {
        "plan_from_strategy": inp.plan,
        "me": {"champion": kit.name, "role": "support" if kit.support else "laner",
               "level": me.get("level"), "hp_percent": me.get("hp_percent"), **kit.me_state(sc, ctx.mi, ctx.now),
               "summoner_spells": {n: ("ready" if sc.ready.get(k) else "not ready") for n, k in zip(inp.summoners, "DF") if n}},
        "visible_units": {l: actions.describe(ctx, l, k, u, inp.opp_name) for l, (k, u) in cands.items()},
        "enemy_tower_is": actions.compass(fx, fy),
        "my_tower_is": actions.compass(-fx, -fy),
        "enemy_minions_on_screen": len(sc.minions),
        "ally_minions_on_screen": sc.allies,
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
    target_probs: dict[str, float] = field(default_factory=dict)
    where_probs: dict[str, float] = field(default_factory=dict)
    distance: float | None = None
    cands: dict = field(default_factory=dict, repr=False)
    menu: list[str] = field(default_factory=list)
    menu_fits: float = 1.0
    state: dict = field(default_factory=dict, repr=False)
    explored: bool = False
    raw: Any = field(default=None, repr=False)

    @property
    def extra(self) -> dict:
        top = lambda d: max(d, key=d.get) if d else None
        return {"target": top(self.target_probs), "where": top(self.where_probs), "distance": self.distance}

    def age(self, now: float) -> float:
        return now - self.state_ts


def _probs(ans) -> dict[str, float]:
    return {k: round(float(v), 3) for k, v in dict(ans.probabilities).items()}


class TacticalBrain:
    """Back-to-back Jev calls on the freshest input. explore: share of picks sampled from Jev's
    action distribution instead of its top choice (bot games), so second-best moves get tried."""

    def __init__(self, max_hz: float = 8.0, explore: float = 0.0) -> None:
        self.client = TypeSafeClient(retry=RetryPolicy(max_retries=0, timeout=1.0))
        self.max_hz = max_hz
        self.explore = explore
        self.tactic: Tactic | None = None
        self._input: TacticInput | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0
        self.calls = 0
        self.errors = 0
        self._rate: list[float] = []
        self.last_error = ""
        self.on_answer = lambda: None

    def publish(self, inp: TacticInput) -> None:
        with self._lock:
            self._input = inp

    def rate(self) -> float:
        now = time.time()
        self._rate = [t for t in self._rate if now - t < 5.0]
        return len(self._rate) / 5.0

    def stop(self) -> None:
        self._stop.set()

    def ask(self, inp: TacticInput) -> Tactic | None:
        t0 = time.time()
        ctx = inp.ctx
        if not ctx.sc.minions and ctx.sc.champ is None and not ctx.ally_units:
            return None
        specs = menu(inp)
        cands = actions.candidates(ctx)
        qs = actions.questions(ctx, specs, cands, ctx.kit.role_text(inp.plan.get("lane", "mid")), inp.opp_name)
        qs["menu_fits"] = Noul(instructions=("Is at least one listed move genuinely good right now? Say false if the right "
                                             "play is something none of them covers."))
        res = self.client.system_one(tactical_state(inp, cands), qs)
        ch = res.choices["action"]
        probs = _probs(ch)
        action, explored = ch.choice, False
        if self.explore and random.random() < self.explore and len(probs) > 1:
            action = random.choices(list(probs), weights=[max(p, 0.02) for p in probs.values()])[0]
            explored = action != ch.choice
        usage = getattr(res, "usage", None)
        self._seq += 1
        return Tactic(
            seq=self._seq, action=action, confidence=float(ch.confidence), probabilities=probs,
            state_ts=inp.ts, latency_ms=(time.time() - t0) * 1000,
            tokens=int(getattr(usage, "input_tokens", 0) or 0),
            target_probs=_probs(res.choices["target"]) if "target" in qs else {},
            where_probs=_probs(res.choices["where"]) if "where" in qs else {},
            distance=float(res.scores["distance"].score) if "distance" in qs else None,
            cands=cands, menu=[s.name for s in specs], menu_fits=float(res.nouls["menu_fits"].noul),
            state=tactical_state(inp, cands), explored=explored, raw=res,
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
                t = self.ask(inp)
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
