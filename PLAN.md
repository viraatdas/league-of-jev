# Plan: Jev plays Yasuo

Jev is TypeSafe's "System One" model. It does not generate text. You send it a
JSON `state` plus a map of typed questions (Choice / Score / Noul) and it returns
typed answers with calibrated probabilities. Measured here: about 200 ms per
request with six questions and about 1,470 input tokens, so one request per
second costs roughly $0.11 per 30-minute game at $0.042 per million tokens.

## What is built (v0, blind)

The harness plays the champion. Jev picks the intent once a second, code does the
mechanics five times a second. No screen reading in v0.

- **Input.** Riot's Live Client Data API on localhost: HP, gold, level, items,
  ability levels, every player's KDA and alive state, events. It gives no
  positions, so position along mid lane is dead reckoned from move speed and
  time, reset at every respawn and completed recall.
- **Jev question pack.** `intent` (farm, trade, all_in, retreat, recall,
  push_tower, group, defend), `danger` 0 to 3, `should_recall`,
  `fight_favorable`, `aggression`, and `next_item` as a Choice over candidates
  code supplies from a Yasuo build list.
- **Safety rules in code, not Jev.** Heavy HP loss at under 45% HP, or any hit at
  under 30%, forces a sticky retreat for 7 s. Danger 2.5+ forces retreat. No
  recall in the middle of the lane and none within 25 s of leaving base.
- **Mechanics with the camera locked.** Forward is a fixed screen diagonal per
  side. Farm is attack-move up the lane bounded at 53% of the way to the enemy
  fountain, with Q fired blind every 1.6 s. Push goes to 57%. Retreat walks back
  to the own tower. Recall stops, presses the recall key and waits 8.6 s,
  aborting on damage. Level-ups follow the standard Yasuo order. Shopping types
  the item into the shop search once its screen position is calibrated.
- **Keys come from League's own config** (`Config/input.ini` or
  `PersistedSettings.json`): abilities, summoners, level-up, attack-move, recall,
  shop, camera lock, and the per-ability quick-cast flags. Defaults until the
  first game writes that file.
- **Input guard.** Clicks and keys are only sent while League is the frontmost
  app, so Cmd-Tab to the terminal and Ctrl-C is always safe.

## What v0 cannot do, and the v1 upgrade

Blind means it cannot target a specific minion or champion, last-hit, dodge, use
E or W or R deliberately, or know an enemy is beside it until the HP drops. The
v1 upgrade is a small perception layer: enemy icons on the minimap by ring colour
for danger, and enemy HP bars on screen for targeting. Both are cheap OpenCV
colour masks and need one screenshot session to tune.

## Policy

Riot's third-party policy (February 10, 2025) bans automated input. The build
is aimed at Practice Tool and custom games against bots. The user has said they
will try normals on their own account; that is their call, and the matchmaking
step is not automated.

## Riot's Live Client Data API

The running game serves a local HTTPS API with a self-signed certificate:

```
https://127.0.0.1:2999/liveclientdata/allgamedata   everything below in one call
https://127.0.0.1:2999/liveclientdata/activeplayer  you: level, gold, HP, stats, ability levels, runes
https://127.0.0.1:2999/liveclientdata/playerlist    all 10 players: champ, level, items, KDA, CS, alive/dead, respawn timer
https://127.0.0.1:2999/liveclientdata/eventdata     kills, turrets, dragons, herald, baron, aces, first blood
https://127.0.0.1:2999/liveclientdata/gamestats     game time, game mode, map
https://127.0.0.1:2999/swagger/v3/openapi.json      schema, used to generate types
```

What it does not give: positions or HP of other players, or enemy cooldowns.
Jev's judgments will be based on levels, items, gold, KDA, alive/dead, timers,
and the event log. That is roughly what a good duo partner reads off the
scoreboard and the minimap anyway. We never derive enemy cooldowns and never
put ads in the overlay.

## References

- TypeSafe docs index: https://docs.typesafe.ai/llms.txt
- System One, state, primitives, fan-out, confidence: https://docs.typesafe.ai
- Riot Live Client Data API: https://developer.riotgames.com/docs/lol
- Riot third-party application policy: https://support.riotgames.com/en-us/riot/events/third-party-applications

## Next

1. Game finishes patching, user logs in, starts Practice Tool as Yasuo, locks the camera.
2. `uv run jev doctor`, then `uv run jev snapshot` to calibrate the minimap square and
   the shop search box in `jev/config.py`.
3. `uv run jev play --dry-run` for a minute, then `uv run jev play`.
4. Tune advance limits, Q cadence, and the safety thresholds from the recorded fixtures.
5. v1 perception if wanted.
