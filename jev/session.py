"""One unattended bot game, end to end: a custom 5v5 against bots with the champion and position,
the harness playing it, a game-time limit, leaving the game, and a score. The overnight loop runs
this back to back and reviews the logs and frames between games."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from jev.lcu import CHAMPIONS, LCU, YASUO
from jev.riot_api import RiotLiveClient

POSITION = {"yasuo": "middle", "leesin": "jungle", "thresh": "utility"}
ROLE = {"middle": "MIDDLE", "jungle": "JUNGLE", "utility": "UTILITY"}
POST_GAME = ("PreEndOfGame", "EndOfGame", "WaitingForStats", "Reconnect")


def _say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def clear_post_game(c: LCU, timeout_s: float = 90.0) -> str:
    """Leave the end-of-game screens (honor vote, stats) until the client is idle."""
    t0 = time.time()
    phase = c.gameflow()
    while phase in POST_GAME and time.time() - t0 < timeout_s:
        if phase == "PreEndOfGame":
            code, seq = c.req("GET", "/lol-pre-end-of-game/v1/currentSequenceEvent")
            name = (seq or {}).get("name") if isinstance(seq, dict) else None
            if name:
                c.req("POST", f"/lol-pre-end-of-game/v1/complete/{name}")
            c.req("POST", "/lol-honor-v2/v1/honor-player", {"honorCategory": "", "summonerId": 0})
        elif phase in ("EndOfGame", "WaitingForStats"):
            c.req("POST", "/lol-end-of-game/v1/state/dismiss-stats")
            c.req("POST", "/lol-lobby/v2/play-again")
        time.sleep(3)
        phase = c.gameflow()
    if phase in ("Lobby",):
        c.delete_lobby()
        phase = c.gameflow()
    return phase


def surrender(riot: RiotLiveClient) -> bool:
    """Type /ff in chat (the only human on the team decides the vote), then wait for the end."""
    from jev.control import Controller, activate_game

    ctl = Controller(dry_run=False, log=lambda m: None)
    for _ in range(3):
        activate_game()
        time.sleep(0.6)
        with ctl.slow():
            ctl.key("return")
            time.sleep(0.4)
            ctl.type_text("/ff", per_char_ms=90)
            time.sleep(0.3)
            ctl.key("return")
        for _ in range(20):
            time.sleep(1.0)
            if not riot.is_game_running():
                return True
            ev = (riot.event_data() or {}).get("Events", [])
            if any(e.get("EventName") == "GameEnd" for e in ev):
                return True
    return False


def kill_game() -> None:
    subprocess.run(["pkill", "-x", "LeagueofLegends"], check=False)


def run(champion: str = "yasuo", minutes: float = 18.0, tag: str = "", difficulty: str = "RSINTERMEDIATE",
        extra_args: list[str] | None = None) -> dict:
    champion = champion.lower().replace(" ", "")
    pos = POSITION.get(champion, "middle")
    tag = tag or time.strftime("%m%d_%H%M") + f"_{champion}"
    logs = Path("logs/night")
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{tag}.log"
    frames = f"snapshots/night/{tag}"
    c = LCU()
    riot = RiotLiveClient()
    phase = clear_post_game(c)
    _say(f"client phase {phase}")
    if riot.is_game_running():
        _say("a game is already running: leaving it first")
        if not surrender(riot):
            kill_game()
            time.sleep(5)
        clear_post_game(c)
    # The harness first, so it is ready (and waiting) when the game window appears.
    cmd = ["uv", "run", "jev", "play", "--logfile", str(log), "--decision-log", str(logs / f"{tag}_decisions.jsonl"),
           "--save-frames", "2", "--frames-dir", frames, "--champion", champion, "--role", ROLE[pos]] + (extra_args or [])
    console = open(logs / f"{tag}.console", "w")
    harness = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
    _say(f"harness pid {harness.pid}: {' '.join(cmd)}")
    started = False
    for line in c.bot_game(CHAMPIONS.get(champion, YASUO), difficulty=difficulty, position=pos):
        _say(f"lcu: {line}")
        started = started or line == "game starting"
    result = {"tag": tag, "champion": champion, "log": str(log), "frames": frames, "ended": "?"}
    try:
        if not started:
            result["ended"] = "no game"
            return result
        t0 = time.time()
        while not riot.is_game_running() and time.time() - t0 < 240:
            time.sleep(2)
        last_note = 0.0
        while True:
            d = riot.all_game_data()
            if d is None:
                time.sleep(3)
                if not riot.is_game_running():
                    result["ended"] = "game over"
                    break
                continue
            gt = float((d.get("gameData") or {}).get("gameTime", 0.0))
            if harness.poll() is not None:
                _say(f"harness exited with {harness.returncode}: restarting it")
                console = open(logs / f"{tag}.console", "a")
                harness = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
            if time.time() - last_note > 60:
                last_note = time.time()
                try:
                    tail = log.read_text(errors="ignore").splitlines()[-1][:160]
                except (OSError, IndexError):
                    tail = "-"
                _say(f"t={int(gt // 60)}:{int(gt % 60):02d} {tail}")
            if gt >= minutes * 60:
                result["ended"] = "time limit"
                break
            time.sleep(5)
    finally:
        try:
            os.killpg(harness.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(2)
    if result["ended"] == "time limit":
        ok = surrender(riot)
        _say(f"surrender {'worked' if ok else 'did not end the game: closing it'}")
        if not ok:
            kill_game()
            time.sleep(8)
    from jev.score import report

    result["score"] = report(log)
    _say("score:\n" + result["score"])
    time.sleep(5)
    _say(f"client phase after: {clear_post_game(c)}")
    c.close()
    return result
