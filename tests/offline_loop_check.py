"""Offline check of the play loop against fixtures with a dry-run controller. Run: uv run python tests/offline_loop_check.py"""
"""Drive Player._tick with fixtures and a dry-run controller; no game needed."""
import time, json
from jev.loop import Player, choose_intent
from jev.mechanics import Mechanics
from jev.riot_api import load_fixture
from jev.state import build_state, Perception
from jev.brain import Decision

p = Player(dry_run=True)
p.mech = Mechanics(p.ctl, p.screen, p.kb, "ORDER")
data = load_fixture("tests/fixtures/midgame_yasuo_vs_zed.json")
data["activePlayer"]["championStats"]["moveSpeed"] = 370.0
data["activePlayer"]["championStats"]["currentHealth"] = 1300.0
now = time.time()
# Fake decisions instead of calling Jev every tick.
def fake(intent, danger=1.0, recall=0.1):
    return Decision(intent, 0.6, {intent: 0.6}, danger, recall, 0.5, 1.0, "Boots", 0.5, 150, "jev-test", 1400)
p.decision = fake("farm")
for i in range(140):  # ~28 s of base -> lane travel at 5 Hz
    perc = p._tick(data, now + i * 0.2)
print("after travel:", p.phase, perc.position, "lane%", p.mech.nav.pct, "intent", p.intent, "last", p.mech.last_action)
assert p.phase == "lane", p.phase
for i in range(140, 170):
    perc = p._tick(data, now + i * 0.2)
print("farming:", "lane%", p.mech.nav.pct, "intent", p.intent, "last", p.mech.last_action)
# Take heavy damage while Jev says the situation is dangerous -> retreat
data["activePlayer"]["championStats"]["currentHealth"] = 500.0
p.decision = fake("farm", danger=2.7)
perc = p._tick(data, now + 170 * 0.2)
print("after burst:", "dmg", round(perc.hp_lost_recent_pct), "intent", p.intent, "last", p.mech.last_action)
assert p.intent == "retreat"
for i in range(171, 200):
    perc = p._tick(data, now + i * 0.2)
print("retreated:", "lane%", p.mech.nav.pct, "intent", p.intent, "last", p.mech.last_action)
assert p.mech.nav.pct < 50 and p.intent == "retreat", (p.mech.nav.pct, p.intent)
# Survival floor without Jev: HP under 15% while being hit -> retreat even if Jev says farm
p.decision = fake("farm", danger=0.5)
data["activePlayer"]["championStats"]["currentHealth"] = 150.0
perc = p._tick(data, now + 200 * 0.2)
assert p.intent == "retreat", p.intent
data["activePlayer"]["championStats"]["currentHealth"] = 500.0
# Recall when safe
p.decision = fake("recall", danger=0.5, recall=0.9)
data["activePlayer"]["championStats"]["currentHealth"] = 500.0
for i in range(200, 260):
    perc = p._tick(data, now + i * 0.2)
print("recall:", "intent", p.intent, "phase", p.phase, "lane%", p.mech.nav.pct, "last", p.mech.last_action)
assert p.phase in ("base", "lane")
assert any("recalling" in a or "key(b)" in a or "click_left(1181,1020)" in a for a in list(p.log_lines)) or p.phase == "base", list(p.log_lines)
# Death resets
data["allPlayers"][0]["isDead"] = True
perc = p._tick(data, now + 261 * 0.2)
print("dead:", p.intent, p.phase, "lane%", p.mech.nav.pct)
data["allPlayers"][0]["isDead"] = False
perc = p._tick(data, now + 262 * 0.2)
print("respawn:", p.intent, p.phase, "last", p.mech.last_action)
print("actions tail:", list(p.log_lines))
print("LOOP OK")
