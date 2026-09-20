# Decisions

Shared, agent-authored log of cross-cutting decisions the fleet must honor. The conductor records plan/rebase/steer decisions here; workers record interface contracts + adjustments. Each entry is a `##` heading with **What / Why / By** so it scans. Re-read before each significant step; jj merges concurrent edits as first-class conflicts on fan-in.

## worker: Installed the TypeSafe Claude Code plugin (typesafe@typesafe-ai 0.5.7). Identif…
- **Did:** Installed the TypeSafe Claude Code plugin (typesafe@typesafe-ai 0.5.7). Identified Jev as TypeSafe's ~100 ms System One judgment model. Wrote PLAN.md: a Riot Live Client Data API poller feeding a per-second Jev question pack with a code-owned policy layer, two modes (coach for Practice Tool / vs AI, safe for item recs and post-game review) because Riot's Feb 2025 policy bans 'drawing conclusions for you during gameplay'. Staged the League installer via brew cask; the GUI installer still needs to be run by the user. Committed and pushed to origin/main at 2313b0b.
- **Interfaces:** PLAN.md (architecture, question pack, cost/latency budget, milestones M0-M4, setup checklist); README.md (description linking PLAN.md); .gitignore (adds .env, node_modules/, fixtures/*.json); Claude Code plugin typesafe@typesafe-ai installed at user scope; Homebrew cask league-of-legends staged at /opt/homebrew/Caskroom/league-of-legends/1.0/
- **By:** worker · 2026-09-20T17:55:33.798Z

