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


CREDIT_WAIT_S = 60.0


def credit_wait(e: Exception) -> float:
    """Seconds to wait before the next call: a 402 (the organization is out of TypeSafe credits) will
    not clear in a second, so the heads ask again once a minute instead of several times a second."""
    return CREDIT_WAIT_S if "402" in str(e) else 0.2

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
    "farm": "Stay with the minion wave and last-hit minions. The default when nothing else is clearly better. "
            "With `situation.enemies_missing_from_map` at 3 or more, farm on our half of the lane.",
    "trade": "Take one short exchange with `lane_opponent` using Q and auto attacks, then back off.",
    "all_in": "Commit to killing `lane_opponent` now: dash in with E, land Q, use R when they are airborne.",
    "retreat": "Walk back toward your own tower immediately because staying is too dangerous "
               "(also when `situation.window` says two or more of us are dead and enemies are near).",
    "recall": "Move to a safe spot and recall to base to spend gold or heal. Best right after our wave is pushed "
              "into their tower, never during a power play.",
    "push_tower": "Push the wave into the enemy tower and attack the tower. Right in a power play "
                  "(`situation.window`: two or more of them dead) or when `lane_opponent` is dead and our wave is there.",
    "group": "Leave lane and join teammates at `destination` for an objective or fight "
             "(in a power play: the tower or dragon they are taking).",
    "defend": "Fall back to protect my own tower or inhibitor that is under attack (at `destination` if it names one of mine).",
    "go_to": "Leave the lane and go to `destination`: another lane, an objective (dragon, baron), the river, a jungle buff, or a tower, to fight, help, take the objective, or ward.",
}


INTENTS_SUPPORT: dict[str, str] = dict(INTENTS, **{
    "farm": "Stay in lane beside your carry (the allied bottom laner): zone the enemy and protect the carry. Do not take last hits.",
    "trade": "Engage `lane_opponent` together with your carry (hook, flay), then back off.",
    "all_in": "Commit with your carry to kill `lane_opponent` now: hook, flay, box.",
})


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
    destination: str | None = None
    destination_probs: dict[str, float] = field(default_factory=dict)
    level_up: str | None = None
    ts: float = 0.0

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


def legal_level_ups(level: int, ranks: dict[str, int]) -> list[str]:
    """Abilities that can take the next point: basics up to rank ceil(level/2) (max 5), the
    ultimate at 6, 11 and 16."""
    if level - sum(ranks.values()) <= 0:
        return []
    out = [a for a in ("Q", "W", "E") if ranks.get(a, 0) < min(5, (level + 1) // 2)]
    if ranks.get("R", 0) < (level >= 6) + (level >= 11) + (level >= 16):
        out.append("R")
    return out


def question_pack(state: dict, role_text: str = "a Yasuo laner in the mid lane", support: bool = False) -> dict:
    """Strategic questions. Itemization is its own head (items.ShopBrain); its current plan
    arrives as state["shopping"] so the recall question can weigh what a trip home would buy.
    `destination` (read when the intent is go_to) widens the action space to the whole map."""
    pack = _core_pack(state, role_text, support)
    places = state.get("map_places")
    if places:
        pack["destination"] = Choice(
            instructions="If I leave my lane (intent go_to), where should I go?",
            criteria=places,
        )
    # (No level_up question: the kit's skill order levels, R at 6/11/16. Jev's picks skipped R, g29.)
    return pack


def _core_pack(state: dict, role_text: str, support: bool) -> dict:
    shopping = state.get("shopping") or {"can_buy_now": [], "note": "no build plan yet"}
    return {
        "intent": Choice(
            instructions=(
                f"You are `me`, {role_text}. Given the current game state, "
                "which single action should I take for the next few seconds?"
            ),
            criteria=INTENTS_SUPPORT if support else INTENTS,
        ),
        "danger": Score(
            instructions="How dangerous is it for `me` to stay where I am for the next few seconds?",
            criteria=[
                "Safe: no enemy champions visible near me and my HP is fine",
                "Caution: an enemy champion is visible but I have HP and minions between us",
                "Dangerous: an enemy champion is close and I am low on HP, or I am past the middle of the lane with "
                "`situation.enemies_missing_from_map` at 3 or more",
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

    def decide(self, state: dict, role_text: str = "a Yasuo laner in the mid lane", support: bool = False) -> Decision:
        t0 = time.perf_counter()
        qs = question_pack(state, role_text, support)
        sent = {k: v for k, v in state.items() if k != "map_places"}  # already the destination criteria
        res = self.client.system_one(sent, qs)
        dt = (time.perf_counter() - t0) * 1000
        intent = res.choices["intent"]
        usage = getattr(res, "usage", None)
        dest = res.choices.get("destination") if hasattr(res.choices, "get") else None
        lvl = res.choices.get("level_up") if hasattr(res.choices, "get") else None
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
            destination=dest.choice if dest is not None else None,
            destination_probs={k: round(float(v), 3) for k, v in dict(dest.probabilities).items()} if dest is not None else {},
            level_up=lvl.choice if lvl is not None else None,
            ts=time.time(),
        )

    def close(self) -> None:
        self.client.close()
