# league-of-jev

Jev, TypeSafe's fast System One model, plays Yasuo. Three Jev heads make every decision; code
reads the game and executes. Riot's local Live Client Data API gives exact stats (HP, gold,
items, levels, events). A light screen read (colour masks, no model) gives positions: the
minimap for macro position, health bars for every unit on screen with its HP, and the HUD
icons for cooldowns.

| Head | Rate | Questions | Answer is used for |
|---|---|---|---|
| Strategy | 1/s, and at once on big HP changes or a level-up | intent (farm, trade, all_in, retreat, recall, push_tower, group, defend, go_to), destination (19 map places), level_up (legal abilities only), danger, should_recall, fight_favorable, aggression | lane mode, map travel, skill order, recalls, how far forward to stand |
| Tactics | back to back, ~6-7/s while units are on screen | action, target, where, distance, menu_fits | the next move, executed within one actor tick |
| Build | every 20 s and on entering base | need_armor, need_magic_resist, need_tenacity, need_antiheal, need_defense_first, next_item over a shortlist from the full Data Dragon catalog (plus Control Wards, trinket swap, elixirs) | what to buy; code buys the item or its best affordable components |

**Tactical action space** (`jev/actions.py`, kits in `jev/kits.py`), filtered each step to what is possible:

- Champion moves. Yasuo: Q / Q3 tornado (aimed), W wind wall, E (through a chosen unit), E then Q, beyblade (E, Q, Flash), R. Thresh: Q hook, Q2 fly, fly then flay pull, flash hook, W lantern, E flay (any direction), E pull, E push, R box.
- Summoner spells from the game API: Flash, Ignite, Exhaust, Heal, Barrier, Ghost, Cleanse, Smite.
- Items: every active the inventory has (targeted ones get a target), potions, trinket and Control Wards.
- Universal: move, attack, attack-move, stop, hold; standing modes farm / push / stay with the carry / back off.
- Heads: `target` (enemy champions, the six most relevant enemy minions, allied champions), `where` (at the target, on myself, up or down the lane, eight compass directions), `distance` (short, medium, full range). The executor masks each head by what the chosen move accepts and takes Jev's most likely valid answer.

The tactical head follows OpenAI Five's action layout (arXiv 1912.06680, Appendix F): a primary
action from a list filtered to what is possible now, plus target parameters read only when the
action needs them. Code-only reflexes: last hits from minion HP trends, R the moment our own
tornado lifts the target, level-ups, and a survival floor that retreats when HP collapses
between Jev answers. Input is capped at about 545 orders a minute; the overlay shows APM.

Perception latency (`tests/bench_perception.py`): ScreenCaptureKit hands over a frame about 2 ms
after capture, and reading it (health bars, HUD, minimap) takes about 7 ms, so a frame is text
about 9 ms after it was captured (the old blocking mss grab: 20-26 ms). The actor wakes on every
new frame and every Jev answer. Code reflexes act within one read of the frame; Jev's moves add
its ~133 ms round trip.

Riot's third-party policy prohibits automated input. Run this in Practice Tool or a custom
game against bots. Queueing it into matchmade games is your account and your call.

## One-time setup

1. League of Legends installed and patched (`/Applications/League of Legends.app`).
2. `TYPESAFE_API_KEY` in `.env` (gitignored). Done.
3. macOS permissions for the terminal you launch from (Ghostty): Accessibility and Screen
   Recording under System Settings > Privacy & Security. Both already granted.
4. `uv sync` once.

Check everything with:

```
uv run jev doctor
uv run jev keys      # key bindings and quick-cast flags read from League's own config
```

Keybinds come from League's `Config/input.ini` / `PersistedSettings.json`, so rebinding in the
client is respected. Until League writes that file (after your first game) defaults are assumed
and `doctor` says so.

## In-game settings the bot assumes

- Camera locked on your champion (press the lock icon next to the minimap, or the camera lock key).
  With the camera locked the champion is always at the screen centre, which is what the blind
  movement relies on.
- Yasuo, mid lane. Blue or red side both work.
- Any window mode is fine. The bot uses whatever resolution the main display has.

## Play

```
uv run jev lcu practice && uv run jev lcu start && uv run jev lcu pick   # Practice Tool lobby, lock Yasuo
uv run jev play --dry-run                                                # prints the clicks and keys it would send
uv run jev play --logfile logs/play.log                                  # sends them; one status line per second
```

The game only accepts input while it is the active window (verified through the game API:
clicks and keys sent while another app is active do nothing, including events posted to
the game's process). `play` therefore keeps the game window in front, re-activating it if
it slips behind, and pauses if it cannot. Leave the machine to it while it plays.

Start a Practice Tool or custom game, lock the camera, then run `play`. It waits for the game
API, walks to lane, farms, fires Q, retreats on damage, recalls when Jev says so, levels
abilities in the standard Yasuo order, and buys the item Jev picks once the shop search box has
been calibrated (see below). Every second's state and Jev's answers are recorded to `fixtures/`.

The overlay (on by default, `--no-overlay` to hide) shows every head's answers: the tactical
menu with a probability bar per move, the chosen move, target, where and distance; the plan,
danger, recall, fight, aggression, map destination and level-up pick; the build and its needs;
APM and screen-to-input latency. A marker layer draws the last move over the game (target
ring, aim line, killable minions, enemy HP); `--no-markers` turns it off. Drag the panel with
Cmd held (or freely while the game is not the active app); double-click for a compact view.
The position is remembered. `uv run jev overlay-demo` shows it with a replayed fight, no game
needed.

Stop it with Ctrl-C in the terminal. It never takes over the mouse permanently; each click is a
single event, so you can grab the mouse back at any time.

## Calibration (first session)

`uv run jev snapshot` saves a screenshot to `snapshots/`. From it, set in `jev/config.py`:

- `Geometry.minimap`: the minimap square (x, y, side) in pixels. Enables minimap-click travel
  to lane and tower instead of dead-reckoned diagonal clicks.
- `Geometry.shop_search`: the shop search box (x, y). Enables buying items.

## Layout

- `jev/riot_api.py` Live Client Data poller and fixture recorder
- `jev/state.py` compact JSON state Jev is asked about
- `jev/brain.py` the strategy question pack and the `Decision` it returns
- `jev/actions.py` the full tactical action space: moves, summoners, items, target / where / distance heads, executor
- `jev/kits.py` champion kits (Yasuo, Thresh): abilities, combos, standing modes, reflexes, item profile
- `jev/places.py` named map places for the strategy head's destination answer
- `jev/tactics.py` the tactical head: menu, state, back-to-back Jev thread
- `jev/items.py` the build head: Data Dragon catalog, need questions, shortlist, recipe-aware purchases
- `jev/capture.py` ScreenCaptureKit stream of the whole display (game view, minimap, HUD in one frame), mss fallback
- `jev/vision.py` health bars (units and HP) and HUD icons (cooldowns) from each frame, ~5 ms
- `jev/minimap.py` minimap reader: own position, minions, champions
- `jev/micro.py` unit tracking, last-hit prediction, Q-stack count, combo executor, reflexes
- `jev/overlay.py` movable panel with every head's answers and probabilities, plus the marker layer
- `jev/overlay_demo.py` replayed fight for `jev overlay-demo`
- `jev/keybinds.py` reads League's input config
- `jev/control.py` Quartz CGEvent mouse and keyboard
- `jev/mechanics.py` macro behaviours: travel, lane position, retreat, recall, level, shop
- `jev/loop.py` threads (API, perception, strategy, tactics, build), 30 Hz actor, safety rules
- `PLAN.md` design notes and policy discussion
