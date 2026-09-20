# Plan: playing League of Legends with Jev

Jev is TypeSafe's "System One" model. It does not generate text. You send it a
JSON `state` plus a map of typed questions (Choice / Score / Noul) and it returns
typed answers with calibrated probabilities. Most requests complete in about
100 ms, and every question in a request is evaluated in parallel against the
same state. That is the reason Jev fits a live game: an LLM coach takes seconds
to think, Jev takes a tenth of a second.

## What "play with Jev" means here

Jev sits next to you as a duo partner in your ear, not on your keyboard.

- What we build: a **live co-pilot** that reads the game state Riot exposes on
  your own machine, asks Jev a batch of judgment questions every second, and
  tells you what to do (recall, trade, group, buy X, contest dragon).
- What we never build: anything that presses keys or moves the mouse. Riot
  prohibits scripting and botting, the Mac client ships with embedded
  Vanguard, and Jev only accepts text so it cannot see the screen anyway.

## The policy catch, read before playing ranked with this

Riot's third-party application policy (last updated February 10, 2025) bans
four things that give "a measurable player advantage":

1. Exposing information that is intentionally obfuscated.
2. Taking actions on your behalf (botting or scripting).
3. Drawing conclusions for you during gameplay.
4. Altering your field of intelligence (zoomhacks, global ult alerts).

Item 3 is the one that matters. A coach that says "recall now" or "this fight
is favorable" is drawing conclusions for you during gameplay. Riot used the
same reasoning to ban enemy ultimate trackers in March 2025. The technical
risk is low: we read an official local API, inject nothing, and send no input,
so Vanguard has nothing to detect. The account-policy risk is real.

So the app has two modes and the plan sequences them:

- **`coach` mode.** Full live advice. Run it in Practice Tool and Co-op vs AI,
  where there is no opponent to disadvantage. Using it in normals or ranked on
  your main account is your call, made with the policy text above in hand.
- **`safe` mode.** Only what Riot's own wording treats as fine: build and item
  recommendations, and a post-game review that replays the recorded game
  through Jev and tells you where you should have recalled, grouped, or
  contested. Same pipeline, advice delivered after the fact.

## Data source: Riot's Live Client Data API

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

## Architecture (one local process, TypeScript)

```
Riot game API ──poll 2-4 Hz──▶ Poller ──▶ State builder ──▶ Jev question pack (1 req/s)
                                              │                      │
                                       fixture recorder        Policy layer
                                                                     │
                                                 ┌───────────────────┼────────────────┐
                                             terminal            macOS `say`      overlay window
```

1. **Poller.** Fetches `allgamedata` and `eventdata`, tolerates the self-signed
   cert for that host only, handles "no game running" by idling.
2. **State builder.** Jev's accuracy drops as irrelevant state grows, so code
   filters first. The state sent each tick is a small JSON object:
   `game` (time, mode), `me` (champ, role, level, gold, HP %, items, ability
   levels), `lane_opponent` (champ, level, items, KDA, alive), `teams` (kills,
   CS and KDA as gold proxies, alive counts, respawn timers), `recent_events`
   (last 60 s), and `timers` (next dragon / baron / herald computed exactly in
   code from the event log, never asked of Jev).
3. **Question pack.** One request per tick using speculative fan-out. First cut:

   | id | type | asks |
   | --- | --- | --- |
   | `next_action` | Choice | farm, trade, all_in, recall, roam, group, contest_objective, defend, ward |
   | `fight_favorable` | Noul | is a 1v1 with `lane_opponent` favorable right now |
   | `should_recall` | Noul | recall now given gold, HP, wave, and timers |
   | `danger` | Score | 0 safe, 1 caution, 2 enemies likely nearby, 3 leave now |
   | `macro_priority` | Choice | dragon, baron, herald, tower, farm, defend |
   | `next_item` | Choice | over 4-6 candidates that code picks from a build table |
   | `contest_next_objective` | Noul | should the team contest the objective spawning next |

   Jev cannot generate an item name, so `next_item` is a Choice over candidates
   code supplies. Every question is worded directly and names the state fields
   it uses in backticks, per the TypeSafe docs. `safe` mode sends only
   `next_item` live and the rest during post-game replay.
4. **Policy layer.** Code owns the decision to speak. It acts only when a
   Choice's confidence clears a threshold, debounces so advice does not flicker,
   rate-limits speech to one line every few seconds, and escalates `danger`
   immediately. Thresholds are tuned on recorded games, not guessed.
5. **Outputs, in build order.** Terminal panel first (fastest to test), then
   voice through the macOS `say` command so you hear "Jev: back off" without
   looking away, then a small always-on-top overlay window.

## Latency and cost

- Poll 100-250 ms, Jev about 100 ms, render under 10 ms. Advice lands within
  roughly 300 ms of a state change.
- State about 1.5k tokens plus questions about 1k, so about 2.5k tokens per
  tick. At 1 tick per second a 30-minute game is about 4.5M tokens.
- Jev 1.13 is $0.042 per million input tokens, so about $0.20 per game.
- Rate limits are 1,200 requests per minute and 250k tokens per second, so a
  single game at 1 request per second is well inside both.

## Testing without a live game

- The fixture recorder saves every `allgamedata` snapshot to `fixtures/` so
  question packs can be replayed offline and thresholds tuned after the fact.
  This is also what powers `safe` mode's post-game review.
- Practice Tool runs the real game client, so the API works there for live
  smoke tests before a real match.
- An eval file holds fixture snapshot plus expected answer pairs. Jev is
  consistent for similar inputs, so regressions show up as probability shifts.

## Milestones

- **M0, now.** Plan, stage the League installer, get a TypeSafe key. This file.
- **M1.** Poller, state builder, fixture recorder. Verify against a Practice Tool game.
- **M2.** Question pack, policy layer, terminal output. Play one Practice Tool
  or Co-op vs AI game with `coach` mode on.
- **M3.** Voice and overlay. Tune thresholds from the recorded games. Ship
  `safe` mode's post-game review.
- **M4.** Champ select assistant using the League Client (LCU) API: Jev picks
  from your champion pool given both team comps and bans. LCU is unofficial
  and unsupported by Riot, so it is last.

## Setup checklist

1. **League of Legends.** The user is downloading the Mac client directly from
   Riot. It runs on Apple silicon and includes embedded Vanguard, no separate
   install.
2. **TypeSafe key.** Done. It lives in the gitignored `.env` as
   `TYPESAFE_API_KEY` and was verified against `GET /v1/models`.
3. **Toolchain.** Node 26, pnpm, and bun are already installed. The app uses
   `@typesafe-ai/sdk` (Node 20+). The TypeSafe Claude Code plugin is installed.

## Open questions (not blocking M1)

- Region: NA assumed. If you play on another server the cask URL changes.
- Champion: Yasuo. Seeds the `next_item` candidates and the champ select pool.
- Voice and overlay are deprioritized. The user wants the harness to play the
  game itself with Jev driving decisions. Open question: which control harness,
  see the harness notes once decided.
- Whether `coach` mode ever runs outside Practice Tool and vs AI on your main.

## References

- TypeSafe docs index: https://docs.typesafe.ai/llms.txt
- System One, state, primitives, fan-out, confidence: https://docs.typesafe.ai
- Riot Live Client Data API: https://developer.riotgames.com/docs/lol
- Riot third-party application policy: https://support.riotgames.com/en-us/riot/events/third-party-applications
