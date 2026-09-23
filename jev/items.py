"""General itemization from Riot's Data Dragon catalog, not a fixed build.

The catalog (every Summoner's Rift item with price, stats, tags and recipe, plus every
champion's class tags) is downloaded once per patch and cached. Jev reads the enemy team
(champions, classes, items, how fed they are) and answers, in one parallel call:
  - need questions (Noul): armor? magic resist? tenacity against CC? anti-heal? defense before damage?
  - next_item (Choice): over a shortlist that code builds from the whole catalog, shaped by the
    previous need answers, plus Yasuo's usual core items and the tier-2 boots.
Code then turns the chosen item into purchases for this base visit: the item itself if the
gold covers what is left of its recipe, otherwise the most valuable affordable component.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

load_dotenv()

DDRAGON = "https://ddragon.leagueoflegends.com"
CACHE = Path.home() / ".cache" / "league-of-jev" / "ddragon"

TIER2_BOOTS = ["Berserker's Greaves", "Plated Steelcaps", "Mercury's Treads", "Boots of Swiftness",
               "Ionian Boots of Lucidity", "Sorcerer's Shoes"]


@dataclass
class ItemProfile:
    """Per-champion prior: always-listed core items, starters, boots, and which catalog items fit."""

    champion: str
    core: list[str]
    starters: list[str]
    boots: list[str]
    support: bool = False
    crit_bonus: bool = False
    note: str = ""


YASUO_PROFILE = ItemProfile("Yasuo", ["Berserker's Greaves", "Immortal Shieldbow", "Infinity Edge", "Blade of The Ruined King",
                                      "Death's Dance", "Guardian Angel"],
                            ["Doran's Blade", "Health Potion"], ["Berserker's Greaves", "Plated Steelcaps", "Mercury's Treads"],
                            crit_bonus=True, note="Yasuo doubles his crit chance")
# Tags that make an item useful on an AD crit fighter; AP/mana/support/jungle items are left out.
USEFUL = {"Damage", "CriticalStrike", "AttackSpeed", "LifeSteal", "OnHit", "Armor", "SpellBlock",
          "Tenacity", "Health", "ArmorPenetration"}
EXCLUDE = {"SpellDamage", "Mana", "ManaRegen", "GoldPer", "Jungle", "Vision", "MagicPenetration"}
# Support items carry the same tags as fighter defense items but give their value to allies.
SUPPORT_ITEMS = {"Bandlepipes", "Zeke's Convergence", "Knight's Vow", "Locket of the Iron Solari", "Redemption",
                 "Mikael's Blessing", "Trailblazer", "Celestial Opposition", "Solstice Sleigh", "Dream Maker",
                 "Bloodsong", "Zaz'Zak's Realmspike"}

NEEDS: dict[str, dict[str, Any]] = {
    "need_armor": {
        "q": "Should {champ}'s next item give armor? Yes when the enemy's damage is mostly physical "
             "(AD assassins, marksmen, fighters) or a physical-damage enemy is fed.",
        "tags": {"Armor"}},
    "need_magic_resist": {
        "q": "Should {champ}'s next item give magic resist? Yes when the enemy's damage is mostly magic "
             "(mages, AP assassins) or a magic-damage enemy is fed.",
        "tags": {"SpellBlock", "MagicResist"}},
    "need_tenacity": {
        "q": "Does the enemy team have heavy crowd control (stuns, roots, charms, knock-ups, suppression) "
             "so tenacity or a cleanse (Mercury's Treads, Mercurial Scimitar) is worth buying?",
        "tags": {"Tenacity"}},
    "need_antiheal": {
        "q": "Do enemies heal or lifesteal a lot (Soraka, Aatrox, Dr. Mundo, Vladimir, lifesteal items) "
             "so Grievous Wounds (anti-heal) is worth buying?",
        "tags": set(), "text": "grievous"},
    "need_defense_first": {
        "q": "Should {champ} buy a defensive item before more damage or utility? Yes when {champ} keeps "
             "dying or the enemies are fed and fights cannot be survived.",
        "tags": {"Armor", "SpellBlock", "Health"}},
}


@dataclass
class Item:
    id: str
    name: str
    price: int
    tags: list[str]
    stats: dict[str, float]
    from_ids: list[str]
    into_ids: list[str]
    depth: int
    text: str

    def brief(self) -> str:
        st = ", ".join(_fmt_stat(k, v) for k, v in self.stats.items() if _fmt_stat(k, v))
        return f"{self.price}g; {st}; {self.text[:150]}"


_STAT_NAMES = {
    "FlatPhysicalDamageMod": "AD", "FlatCritChanceMod": "crit", "PercentAttackSpeedMod": "attack speed",
    "FlatArmorMod": "armor", "FlatSpellBlockMod": "magic resist", "FlatHPPoolMod": "health",
    "PercentLifeStealMod": "lifesteal", "FlatMovementSpeedMod": "move speed", "PercentMovementSpeedMod": "move speed",
}


def _fmt_stat(k: str, v: float) -> str:
    name = _STAT_NAMES.get(k)
    if not name:
        return ""
    if k.startswith("Percent") or k == "FlatCritChanceMod":
        return f"+{int(round(v * 100))}% {name}"
    return f"+{int(v)} {name}"


def _clean(desc: str) -> str:
    t = re.sub(r"<br\s*/?>", " ", desc or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


class Catalog:
    def __init__(self, version: str | None = None) -> None:
        self.version, item_json, champ_json = self._load(version)
        self.items: dict[str, Item] = {}
        for iid, v in item_json["data"].items():
            if not v.get("maps", {}).get("11") or not v.get("gold", {}).get("purchasable"):
                continue
            if v.get("inStore") is False or v.get("requiredChampion") or v.get("requiredAlly"):
                continue
            self.items[iid] = Item(
                id=iid, name=v["name"], price=int(v["gold"]["total"]), tags=list(v.get("tags", [])),
                stats=dict(v.get("stats", {})), from_ids=list(v.get("from", [])), into_ids=list(v.get("into", [])),
                depth=int(v.get("depth", 1)), text=_clean(v.get("plaintext") or "") or _clean(v.get("description", ""))[:200],
            )
            self.items[iid].full_text = _clean(v.get("description", "")).lower()  # type: ignore[attr-defined]
        self.by_name: dict[str, Item] = {}
        for it in self.items.values():
            self.by_name.setdefault(it.name.lower(), it)
        self.champs: dict[str, dict] = {v["name"].lower(): v for v in champ_json["data"].values()}
        for v in champ_json["data"].values():
            self.champs.setdefault(v["id"].lower(), v)

    # -- loading ---------------------------------------------------------------------
    @staticmethod
    def _load(version: str | None):
        try:
            if version is None:
                version = httpx.get(f"{DDRAGON}/api/versions.json", timeout=5).json()[0]
            d = CACHE / version
            if not (d / "item.json").exists():
                d.mkdir(parents=True, exist_ok=True)
                for f in ("item", "champion"):
                    r = httpx.get(f"{DDRAGON}/cdn/{version}/data/en_US/{f}.json", timeout=15)
                    r.raise_for_status()
                    (d / f"{f}.json").write_text(r.text)
        except Exception:  # noqa: BLE001  offline: newest cached patch
            cached = sorted(CACHE.glob("*/item.json"))
            if not cached:
                raise
            d = cached[-1].parent
            version = d.name
        return version, json.loads((d / "item.json").read_text()), json.loads((d / "champion.json").read_text())

    # -- queries -----------------------------------------------------------------------
    def get(self, name: str) -> Item | None:
        return self.by_name.get(name.lower())

    def completed(self, with_boots: bool = True, profile: ItemProfile = YASUO_PROFILE) -> list[Item]:
        """Finished items that fit the champion: no further upgrade (or tier-2 boots). For a
        fighter, offensive items qualify by tag and defensive ones need health plus armor or
        magic resist, without team auras. For a support, tank and aura items qualify and damage
        items do not. One entry per name."""
        out: dict[str, Item] = {}
        offensive = {"Damage", "CriticalStrike", "AttackSpeed", "OnHit", "LifeSteal", "ArmorPenetration"}
        tanky = {"Health", "Armor", "SpellBlock"}
        for it in self.items.values():
            tags = set(it.tags)
            if it.name in TIER2_BOOTS:
                if with_boots and it.name in profile.boots:
                    out.setdefault(it.name, it)
                continue
            if tags & {"Boots", "Consumable", "Lane", "Trinket"}:
                continue
            if it.into_ids or it.price < 2000:
                continue
            if profile.support:
                if tags & {"CriticalStrike", "LifeSteal", "SpellDamage", "Jungle"} or not (tags & tanky):
                    continue
            else:
                if "Aura" in tags or it.name in SUPPORT_ITEMS or (tags & EXCLUDE) or it.price < 2200:
                    continue
                if not (tags & offensive) and not ("Health" in tags and tags & {"Armor", "SpellBlock"}):
                    continue
            if it.name not in out or it.price < out[it.name].price:
                out[it.name] = it
        return list(out.values())

    def champion_brief(self, name: str) -> dict:
        c = self.champs.get((name or "").lower())
        if not c:
            return {"class": "unknown"}
        info = c.get("info", {})
        dmg = "magic" if info.get("magic", 0) > info.get("attack", 0) + 1 else "physical" if info.get("attack", 0) > info.get("magic", 0) + 1 else "mixed"
        return {"class": "/".join(c.get("tags", [])), "damage": dmg}

    def cost_to_finish(self, item: Item, owned: list[str]) -> int:
        """Gold still needed for `item`, counting owned components that the recipe consumes."""
        pool = [o.lower() for o in owned]

        def cost(it: Item) -> int:
            if it.name.lower() in pool:
                pool.remove(it.name.lower())
                return 0
            comps = [self.items[c] for c in it.from_ids if c in self.items]
            own_part = it.price - sum(c.price for c in comps)
            return own_part + sum(cost(c) for c in comps)

        return cost(item)

    def purchases(self, target: Item, owned: list[str], gold: float, limit: int = 3) -> list[Item]:
        """What to buy this visit toward `target`: the item if affordable, else the most
        valuable affordable components in its recipe tree, until the gold runs out."""
        owned = list(owned)
        buys: list[Item] = []
        g = gold
        for _ in range(limit):
            need = self.cost_to_finish(target, owned)
            if need <= g:
                buys.append(target)
                break
            best: Item | None = None

            def walk(it: Item) -> None:
                nonlocal best
                for c in it.from_ids:
                    ci = self.items.get(c)
                    if ci is None:
                        continue
                    if ci.name.lower() in [o.lower() for o in owned]:
                        continue
                    if self.cost_to_finish(ci, owned) <= g and (best is None or ci.price > best.price):
                        best = ci
                    walk(ci)

            walk(target)
            if best is None:
                break
            buys.append(best)
            owned.append(best.name)
            g -= self.cost_to_finish(best, owned[:-1])
        return buys


def enemy_team(data: dict, catalog: Catalog, my_team: str) -> list[dict]:
    out = []
    for p in data.get("allPlayers", []):
        if p.get("team") == my_team:
            continue
        s = p.get("scores", {})
        out.append({
            "champion": p.get("championName"),
            **catalog.champion_brief(p.get("championName", "")),
            "level": p.get("level"),
            "kda": f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
            "items": [i.get("displayName") for i in p.get("items", []) if i.get("displayName")],
        })
    return out


def shortlist(catalog: Catalog, owned: list[str], needs: dict[str, float], game_min: float, gold: float, n: int = 12,
              profile: ItemProfile = YASUO_PROFILE) -> list[Item]:
    """Candidates for next_item: the champion's core, tier-2 boots if none owned, and the catalog
    items that best match the latest need answers (weighted by how strongly each was answered)."""
    owned_l = {o.lower() for o in owned}
    has_boots = any(b.lower() in owned_l for b in TIER2_BOOTS) or any(
        "Boots" in (catalog.get(o).tags if catalog.get(o) else []) and catalog.get(o).price > 300 for o in owned)
    picks: list[Item] = []

    def add(it: Item | None) -> None:
        if it is not None and it.name.lower() not in owned_l and it not in picks:
            picks.append(it)

    TRINKETS = ("stealth ward", "oracle lens", "farsight alteration")
    if game_min < 1.5 and not [o for o in owned_l if o not in TRINKETS]:
        # (The trinket everyone spawns with does not count: with it, no starter was ever offered
        # and a jungler bought toward Eclipse instead of his pet.)
        for s in profile.starters:
            add(catalog.get(s))
    if not has_boots:
        for b in profile.boots[:3]:
            add(catalog.get(b))
    for c in profile.core:
        if c not in TIER2_BOOTS:
            add(catalog.get(c))
    # Consumables and trinket swaps are part of the build decision too.
    if game_min > 3 and not any("control ward" in o for o in owned_l):
        add(catalog.get("Control Ward"))
    if profile.support and game_min > 6 and "oracle lens" not in owned_l:
        add(catalog.get("Oracle Lens"))
    finished = sum(1 for o in owned if (catalog.get(o) and catalog.get(o).price >= 2200))
    if game_min > 25 or finished >= 5:
        add(catalog.get("Elixir of Iron" if profile.support else "Elixir of Wrath"))

    def score(it: Item) -> float:
        tags = set(it.tags)
        sc = 0.0
        for key, spec in NEEDS.items():
            w = needs.get(key, 0.0)
            if spec.get("tags") and tags & spec["tags"]:
                sc += w
            if spec.get("text") and spec["text"] in getattr(it, "full_text", ""):
                sc += w
        if not profile.support and tags & {"Damage", "CriticalStrike"}:
            sc += 0.3 * (1.0 - needs.get("need_defense_first", 0.0))
        if profile.support and tags & {"Aura", "Active"}:
            sc += 0.3  # team utility
        if profile.crit_bonus and "CriticalStrike" in tags:
            sc += 0.2
        return sc

    ranked = sorted(catalog.completed(with_boots=not has_boots, profile=profile), key=score, reverse=True)
    for it in ranked:
        if len(picks) >= n:
            break
        add(it)
    return picks[:n]


@dataclass
class BuildPlan:
    target: str
    confidence: float
    probabilities: dict[str, float]
    needs: dict[str, float]
    buy_now: list[str]
    buy_now_cost: int
    ts: float
    latency_ms: float
    shortlist: list[str] = field(default_factory=list)

    def summary(self) -> str:
        need = " ".join(f"{k.replace('need_', '')}={v:.2f}" for k, v in self.needs.items())
        return f"build -> {self.target} (p={self.confidence:.2f}) now: {', '.join(self.buy_now) or '-'} | {need}"


class ShopBrain:
    """The itemization head: needs + next_item in one Jev call."""

    def __init__(self, catalog: Catalog | None = None, profile: ItemProfile = YASUO_PROFILE) -> None:
        self.catalog = catalog or Catalog()
        self.profile = profile
        self.client = TypeSafeClient(retry=RetryPolicy(max_retries=1, backoff_max=0.2, timeout=3.0))
        self.needs: dict[str, float] = {k: 0.3 for k in NEEDS}
        self.plan: BuildPlan | None = None
        self.lock = threading.Lock()

    def decide(self, data: dict, my_team: str) -> BuildPlan:
        prev = self.plan
        prior = dict(self.needs)
        plan = self._decide(data, my_team)
        if max(abs(plan.needs[k] - prior.get(k, 0.3)) for k in plan.needs) > 0.3:
            plan = self._decide(data, my_team)  # the shortlist was built on stale needs: ask again
        return self._stick(prev, plan, data, my_team)

    def _components(self, item: Item) -> set[str]:
        out: set[str] = set()
        for c in item.from_ids:
            ci = self.catalog.items.get(c)
            if ci is not None:
                out.add(ci.name)
                out |= self._components(ci)
        return out

    def _stick(self, prev: "BuildPlan | None", plan: "BuildPlan", data: dict, my_team: str) -> "BuildPlan":
        """Finish what was started: when we own a component of the previous target and have not
        completed it, keep that target unless Jev now clearly prefers something else (by 0.15).
        Close calls otherwise flip-flop and buy pieces of two different items."""
        if prev is None or prev.target == plan.target:
            return plan
        target = self.catalog.get(prev.target)
        if target is None:
            return plan
        ap = data.get("activePlayer", {})
        me = next((p for p in data.get("allPlayers", []) if p.get("team") == my_team and
                   (p.get("riotId") == ap.get("riotId") or p.get("summonerName") == ap.get("summonerName"))), {})
        owned = [i.get("displayName") for i in me.get("items", []) if i.get("displayName")]
        started = any(o in self._components(target) for o in owned)
        if prev.target in owned or not started:
            return plan
        if plan.probabilities.get(plan.target, 0) - plan.probabilities.get(prev.target, 0) >= 0.15:
            return plan
        gold = float(ap.get("currentGold", 0.0))
        buys = self.catalog.purchases(target, owned, gold)
        stuck = BuildPlan(target=target.name, confidence=plan.probabilities.get(target.name, prev.confidence),
                          probabilities=plan.probabilities, needs=plan.needs, buy_now=[b.name for b in buys],
                          buy_now_cost=sum(b.price for b in buys), ts=plan.ts, latency_ms=plan.latency_ms,
                          shortlist=plan.shortlist)
        with self.lock:
            self.plan = stuck
        return stuck

    def _decide(self, data: dict, my_team: str) -> BuildPlan:
        t0 = time.time()
        ap = data.get("activePlayer", {})
        me = next((p for p in data.get("allPlayers", []) if p.get("team") == my_team and
                   (p.get("riotId") == ap.get("riotId") or p.get("summonerName") == ap.get("summonerName"))), {})
        owned = [i.get("displayName") for i in me.get("items", []) if i.get("displayName")]
        gold = float(ap.get("currentGold", 0.0))
        gmin = float((data.get("gameData") or {}).get("gameTime", 0.0)) / 60
        cands = shortlist(self.catalog, owned, self.needs, gmin, gold, profile=self.profile)
        champ = self.profile.champion
        s = me.get("scores", {})
        state = {
            "me": {"champion": champ, "role": "support" if self.profile.support else "carry", "level": ap.get("level"), "gold": int(gold), "items": owned,
                   "kda": f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}"},
            "enemy_team": enemy_team(data, self.catalog, my_team),
            "game_minutes": round(gmin, 1),
        }
        qs: dict[str, Any] = {k: Noul(instructions=spec["q"].replace("{champ}", champ)) for k, spec in NEEDS.items()}
        qs["next_item"] = Choice(
            instructions=(f"Which item should {champ} build next? Consider what the enemy team deals and does "
                          f"(damage type, crowd control, healing, who is fed), what {champ} already owns"
                          f"{', and that ' + self.profile.note if self.profile.note else ''}. "
                          + ("" if self.profile.support else
                             "A carry's first legendary item is usually a damage item; boots and defensive items "
                             "come after it unless an enemy is already fed. ")
                          + "Prices are full prices; components already owned count."),
            criteria={it.name: it.brief() for it in cands},
        )
        res = self.client.system_one(state, qs)
        needs = {k: round(float(res.nouls[k].noul), 2) for k in NEEDS}
        ch = res.choices["next_item"]
        target = self.catalog.get(ch.choice) or cands[0]
        buys = self.catalog.purchases(target, owned, gold)
        plan = BuildPlan(
            target=target.name, confidence=float(ch.confidence),
            probabilities=dict(sorted(((k, round(float(v), 3)) for k, v in dict(ch.probabilities).items()), key=lambda kv: -kv[1])[:6]),
            needs=needs, buy_now=[b.name for b in buys], buy_now_cost=sum(b.price for b in buys),
            ts=time.time(), latency_ms=(time.time() - t0) * 1000, shortlist=[c.name for c in cands],
        )
        with self.lock:
            self.needs = needs  # shapes the next shortlist
            self.plan = plan
        return plan
