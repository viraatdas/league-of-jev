# league-of-jev

Jev, TypeSafe's fast System One model, plays Yasuo. The bot reads the game through Riot's
local Live Client Data API, asks Jev a pack of typed questions once a second (about 200 ms
round trip), and turns the chosen intent into mouse and keyboard input on macOS. No screen
reading in v0: movement is blind clicks along the mid-lane diagonal, attacks are attack-move,
Q is fired up the lane, and damage is noticed from HP deltas.

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
uv run jev play --dry-run   # prints the clicks and keys it would send
uv run jev play             # sends them
```

Start a Practice Tool or custom game, lock the camera, then run `play`. It waits for the game
API, walks to lane, farms, fires Q, retreats on damage, recalls when Jev says so, levels
abilities in the standard Yasuo order, and buys the item Jev picks once the shop search box has
been calibrated (see below). Every second's state and Jev's answers are recorded to `fixtures/`.

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
- `jev/brain.py` the Yasuo question pack and the `Decision` it returns
- `jev/keybinds.py` reads League's input config
- `jev/control.py` Quartz CGEvent mouse and keyboard
- `jev/mechanics.py` blind behaviours: go lane, farm, trade, push, retreat, recall, level, shop
- `jev/loop.py` main loop, safety rules, Jev thread, terminal panel
- `PLAN.md` design notes and policy discussion
