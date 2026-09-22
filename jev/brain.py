"""The Jev question pack. One request per tick, every question answered in parallel.

Jev returns typed judgments with probabilities. Code (mechanics.py) turns the chosen
intent into clicks and keys. Item choice is a Choice over candidates code supplies,
because Jev cannot generate an item name.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

load_dotenv()

# Simplified Yasuo build path. Edit freely; the current patch's item names matter.
YASUO_BUILD = [
    "Berserker's Greaves",
    "Immortal Shieldbow",
    "Infinity Edge",
    "Bloodthirster",
    "Mortal Reminder",
    "Guardian Angel",
]
YASUO_EARLY = ["Doran's Blade", "Health Potion", "Noonquiver", "Boots"]
# Approximate prices; check against the current patch.
ITEM_PRICES = {
    "Doran's Blade": 450, "Health Potion": 50, "Noonquiver": 1300, "Boots": 300,
    "Berserker's Greaves": 1100, "Immortal Shieldbow": 3000, "Infinity Edge": 3450,
    "Bloodthirster": 3400, "Mortal Reminder": 3300, "Guardian Angel": 3200,
}

INTENTS: dict[str, str] = {
    "farm": "Stay with the minion wave and last-hit minions. The default when nothing else is clearly better.",
    "trade": "Take one short exchange with `lane_opponent` using Q and auto attacks, then back off.",
    "all_in": "Commit to killing `lane_opponent` now: dash in with E, land Q, use R when they are airborne.",
    "retreat": "Walk back toward your own tower immediately because staying is too dangerous.",
    "recall": "Move to a safe spot and recall to base to spend gold or heal.",
    "push_tower": "Push the wave into the enemy tower and attack the tower.",
    "group": "Leave lane and join teammates for an objective or fight.",
    "defend": "Fall back to protect your own tower or inhibitor that is under attack.",
}


@dataclass
class Decision:
    intent: str
    intent_confidence: float
    intent_probabilities: dict[str, float]
    danger: float
    should_recall: float
    fight_favorable: float
    aggression: float
    next_item: str | None
    next_item_confidence: float
    latency_ms: float
    model: str
    input_tokens: int
    raw: Any = field(default=None, repr=False)

    def summary(self) -> str:
        return (
            f"{self.intent} (p={self.intent_confidence:.2f}) danger={self.danger:.1f} "
            f"recall={self.should_recall:.2f} fight={self.fight_favorable:.2f} "
            f"aggr={self.aggression:.1f} item={self.next_item} [{self.latency_ms:.0f} ms, {self.input_tokens} tok]"
        )


def item_candidates(owned: list[str]) -> list[str]:
    owned_l = {o.lower() for o in owned}
    if any("greaves" in o for o in owned_l):
        owned_l.add("boots")
    remaining = [i for i in YASUO_EARLY + YASUO_BUILD if i.lower() not in owned_l]
    return remaining[:5] or ["Elixir of Wrath"]


def candidates_with_prices(owned: list[str], gold: float) -> list[dict]:
    return [{"item": c, "price": ITEM_PRICES.get(c), "affordable_now": ITEM_PRICES.get(c, 10**9) <= gold} for c in item_candidates(owned)]


def question_pack(state: dict) -> dict:
    """Strategic questions. Itemization is its own head (items.ShopBrain); its current plan
    arrives as state["shopping"] so the recall question can weigh what a trip home would buy."""
    shopping = state.get("shopping") or {"can_buy_now": [], "note": "no build plan yet"}
    return {
        "intent": Choice(
            instructions=(
                "You are `me`, a Yasuo player in the middle lane. Given the current game state, "
                "which single action should Yasuo take for the next few seconds?"
            ),
            criteria=INTENTS,
        ),
        "danger": Score(
            instructions="How dangerous is it for `me` to stay where I am for the next few seconds?",
            criteria=[
                "Safe: no enemy champions visible near me and my HP is fine",
                "Caution: an enemy champion is visible but I have HP and minions between us",
                "Dangerous: an enemy champion is close and I am low on HP, or several enemies are missing from the minimap",
                "Leave now: I will very likely die if I stay",
            ],
        ),
        "should_recall": Noul(
            instructions={
                "shopping": shopping,
                "question": (
                    "Should `me` recall to base now? Recalling is right when `shopping.can_buy_now` holds a real "
                    "upgrade (a finished item or a strong component toward `shopping.building_toward`), or "
                    "`me.hp_percent` is low, or `lane_opponent` is dead. Staying is right when nothing useful "
                    "can be bought yet and HP is fine."
                ),
            },
            criteria={"true": "Recalling now is clearly right", "false": "Stay in lane"},
        ),
        "fight_favorable": Noul(
            instructions=(
                "Would `me` win a 1v1 fight against `lane_opponent` right now, based on levels, items, "
                "HP, and who is nearby?"
            ),
        ),
        "aggression": Score(
            instructions="How aggressively should `me` play the lane for the next minute?",
            criteria=[
                "Passive: only farm, avoid all trades",
                "Balanced: farm and take safe trades",
                "Aggressive: look for kills and pressure the opponent",
            ],
        ),
    }


class Brain:
    def __init__(self, model: str | None = None, timeout: float = 2.0) -> None:
        kwargs: dict[str, Any] = {"retry": RetryPolicy(max_retries=1, backoff_max=0.2, timeout=timeout)}
        if model:
            kwargs["model"] = model
        self.client = TypeSafeClient(**kwargs)

    def decide(self, state: dict) -> Decision:
        t0 = time.perf_counter()
        res = self.client.system_one(state, question_pack(state))
        dt = (time.perf_counter() - t0) * 1000
        intent = res.choices["intent"]
        usage = getattr(res, "usage", None)
        return Decision(
            intent=intent.choice,
            intent_confidence=float(intent.confidence),
            intent_probabilities={k: round(float(v), 3) for k, v in dict(intent.probabilities).items()},
            danger=float(res.scores["danger"].score),
            should_recall=float(res.nouls["should_recall"].noul),
            fight_favorable=float(res.nouls["fight_favorable"].noul),
            aggression=float(res.scores["aggression"].score),
            next_item=None,
            next_item_confidence=0.0,
            latency_ms=dt,
            model=str(getattr(res, "model", "?")),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            raw=res,
        )

    def close(self) -> None:
        self.client.close()
