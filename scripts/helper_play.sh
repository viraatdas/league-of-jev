#!/bin/sh
# Runs the harness inside offstage's helper account (computeruse), where League plays on a desktop
# nobody watches. Start it with:
#   offstage run --lane session --timeout 14400000 -- /Users/viraat/code/league-of-jev/scripts/helper_play.sh
# Logs, the decision log and saved frames go to /tmp/jev-helper, readable from both accounts.
umask 022
mkdir -p /tmp/jev-helper/run
cd /tmp/jev-helper/run || exit 1
exec /Users/viraat/code/league-of-jev/.venv/bin/jev play \
  --logfile /tmp/jev-helper/play.log \
  --decision-log /tmp/jev-helper/decisions.jsonl \
  --save-frames 2 --forever "$@"
