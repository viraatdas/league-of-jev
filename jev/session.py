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
            if not game_alive():
                return True
            ev = (riot.event_data() or {}).get("Events", [])
            if any(e.get("EventName") == "GameEnd" for e in ev):
                return True
    return False


def kill_game() -> None:
    subprocess.run(["pkill", "-x", "LeagueofLegends"], check=False)


def game_alive() -> bool:
    """The game process exists (the live API alone stalls for seconds under load)."""
    return subprocess.run(["pgrep", "-x", "LeagueofLegends"], capture_output=True).returncode == 0


def harness_alive() -> bool:
    return subprocess.run(["pgrep", "-f", "jev pla[y]"], capture_output=True).returncode == 0


def stop_harness() -> None:
    subprocess.run(["pkill", "-f", "jev pla[y]"], check=False)


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
    cmd = ["uv", "run", "jev", "play", "--logfile", str(log), "--decision-log", str(logs / f"{tag}_decisions.jsonl"),
           "--save-frames", "2", "--frames-dir", frames, "--champion", champion, "--role", ROLE[pos]] + (extra_args or [])

    def start_harness() -> None:
        console = open(logs / f"{tag}.console", "a")
        h = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
        _say(f"harness pid {h.pid}: {' '.join(cmd)}")

    result = {"tag": tag, "champion": champion, "log": str(log), "frames": frames, "ended": "?"}
    if game_alive():
        _say("a game is already running: supervising it")  # attach (e.g. after a runner restart)
    else:
        phase = clear_post_game(c)
        _say(f"client phase {phase}")
        # The harness first, so it is ready (and waiting) when the game window appears.
        if not harness_alive():
            start_harness()
        started = False
        for line in c.bot_game(CHAMPIONS.get(champion, YASUO), difficulty=difficulty, position=pos):
            _say(f"lcu: {line}")
            started = started or line == "game starting"
        if not started:
            result["ended"] = "no game"
            stop_harness()
            return result
        t0 = time.time()
        while not game_alive() and time.time() - t0 < 240:
            time.sleep(2)
    if not harness_alive():
        start_harness()
    last_note, gone, gt = 0.0, 0, 0.0
    try:
        while True:
            time.sleep(5)
            if not game_alive():
                gone += 1
                if gone >= 3:
                    result["ended"] = "game over"
                    break
                continue
            gone = 0
            d = riot.all_game_data()
            if d is not None:
                gt = float((d.get("gameData") or {}).get("gameTime", 0.0))
            if not harness_alive():
                _say("harness not running: restarting it")
                start_harness()
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
            ff = Path("logs/night/FF")
            if ff.exists() and gt >= 15 * 60:
                # Operator's early surrender (a broken lineup, a game with nothing left to learn).
                ff.unlink(missing_ok=True)
                _say("FF file: surrendering this game")
                result["ended"] = "time limit"
                break
    finally:
        stop_harness()
        time.sleep(2)
    if result["ended"] == "time limit":
        ok = surrender(riot)
        _say(f"surrender {'worked' if ok else 'did not end the game: closing it'}")
        if not ok:
            kill_game()
            time.sleep(8)
    t0 = time.time()
    while game_alive() and time.time() - t0 < 45:
        time.sleep(2)
    if game_alive():
        kill_game()  # the victory / defeat screen waits for a click; closing the game is the same
        time.sleep(8)
    from jev.score import report

    result["score"] = report(log)
    _say("score:\n" + result["score"])
    time.sleep(5)
    _say(f"client phase after: {clear_post_game(c)}")
    c.close()
    return result


RIOT_LOCKFILE = Path.home() / "Library/Application Support/Riot Games/Riot Client/Config/lockfile"


def client_ready() -> bool:
    """Logged in to League (the summoner resolves) and a lobby can be made."""
    try:
        c = LCU()
        code, _ = c.req("GET", "/lol-summoner/v1/current-summoner")
        if code != 200:
            return False
        if c.gameflow() in ("InProgress", "GameStart", "ChampSelect"):
            return True
        code, _ = c.create_custom_vs_bots()
        c.delete_lobby()
        return code < 400
    except Exception:  # noqa: BLE001  (no lockfile, client down)
        return False


LOL_DIR = Path("/Applications/League of Legends.app/Contents/LoL")


def refresh_patched_binaries(hours: float = 24.0) -> int:
    """Copy every Mach-O file the patcher rewrote in the last `hours` onto a fresh inode.
    A signed binary rewritten in place keeps the kernel's cached signature for that file, so it
    is killed at launch ("load code signature error 2", exit 9 after 67 ms; patch 16.19 night)."""
    cutoff = time.time() - hours * 3600
    n = 0
    for f in LOL_DIR.rglob("*"):
        try:
            if not f.is_file() or f.is_symlink() or f.stat().st_mtime < cutoff:
                continue
            with open(f, "rb") as fh:
                magic = fh.read(4)
            if magic not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe"):
                continue
            tmp = f.with_name(f.name + ".jevtmp")
            subprocess.run(["cp", "-p", str(f), str(tmp)], check=True)
            os.replace(tmp, f)
            n += 1
        except (OSError, subprocess.CalledProcessError):
            continue
    return n


def relaunch_client() -> str:
    """Quit the League client and start it again through the Riot Client (an expired League
    token, or a login queue that was down, clears this way once the servers are back)."""
    import httpx

    try:
        LCU().req("POST", "/process-control/v1/process/quit")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(10)
    try:
        _, _, port, pw, _ = RIOT_LOCKFILE.read_text().split(":")
        r = httpx.post(f"https://127.0.0.1:{port}/product-launcher/v1/products/league_of_legends/patchlines/live",
                       auth=("riot", pw), verify=False, timeout=10)
        return f"launch {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"launch failed: {e}"


def night(rotation: str = "yasuo,leesin", minutes: float = 16.0, games: int = 30) -> None:
    """Back-to-back games, each in its own `jev session` process so every game runs the latest
    code. The rotation is re-read before each game from logs/night/rotation.txt when it exists
    (comma separated); creating logs/night/STOP ends the loop after the current game. A game
    already running is supervised under its own tag first."""
    import re
    import sys

    logs = Path("logs/night")
    logs.mkdir(parents=True, exist_ok=True)
    for i in range(games):
        stop = logs / "STOP"
        if stop.exists():
            _say("STOP file found: ending the night loop")
            stop.unlink()
            return
        if not game_alive():
            waited = 0
            while not client_ready():
                # Servers down (login queue), token expired, client gone: relaunch every 5 minutes;
                # from the second try on, also refresh binaries a patch rewrote in place.
                if waited >= 5:
                    _say(f"refreshed {refresh_patched_binaries()} patched binaries")
                _say(f"client not ready ({waited} min): relaunching League: {relaunch_client()}")
                for _ in range(10):
                    time.sleep(30)
                    if client_ready():
                        break
                waited += 5
                if (logs / "STOP").exists():
                    break
        tags = sorted((f.stem for f in logs.glob("g*_*.log")), key=lambda t: int(re.match(r"g(\d+)_", t).group(1)))
        if game_alive() and tags:
            tag = tags[-1]
            champ = tag.split("_")[1]
            diff = "RSINTERMEDIATE"
            _say(f"=== supervising the running game {tag} ===")
        else:
            rot_file = logs / "rotation.txt"
            rot = [r.strip() for r in (rot_file.read_text() if rot_file.exists() else rotation).split(",") if r.strip()]
            n = (int(re.match(r"g(\d+)_", tags[-1]).group(1)) if tags else 0) + 1
            champ = rot[(n - 1) % len(rot)]
            # Bot difficulty, re-read each game from logs/night/difficulty.txt (RSINTRO, RSBEGINNER,
            # RSINTERMEDIATE); a game below intermediate carries it in its tag so results never mix.
            diff_file = logs / "difficulty.txt"
            diff = (diff_file.read_text().strip().upper() if diff_file.exists() else "") or "RSINTERMEDIATE"
            tag = f"g{n:02d}_{champ}" + ("" if diff == "RSINTERMEDIATE" else f"_{diff[2:].lower()}")
            _say(f"=== game {tag} ({diff}) ===")
        mins_file = logs / "minutes.txt"  # game time limit, re-read each game (a limit ends the game with /ff)
        try:
            game_minutes = float(mins_file.read_text().strip()) if mins_file.exists() else minutes
        except ValueError:
            game_minutes = minutes
        r = subprocess.run(["uv", "run", "jev", "session", "--champion", champ, "--minutes", str(game_minutes), "--tag", tag,
                            "--difficulty", diff],
                           stdout=sys.stdout, stderr=subprocess.STDOUT)
        _say(f"=== {tag} session exited with {r.returncode} ===")
        if not (logs / f"{tag}.log").exists():
            time.sleep(60)  # no game was played (lobby or pick failed): do not spin
        if r.returncode != 0:
            stop_harness()
            if game_alive():
                kill_game()
            time.sleep(20)


def normal_game(tag: str = "", first: str = "MIDDLE", second: str = "JUNGLE", max_minutes: float = 60.0) -> dict:
    """One normal (PvP) game end to end: queue with position preferences, accept, ban, pick by the
    assigned position, the harness playing the champion and role the game gives it, and the game
    played to its end: no surrender (in PvP /ff starts a vote for four real teammates)."""
    logs = Path("logs/night")
    logs.mkdir(parents=True, exist_ok=True)
    tag = tag or time.strftime("normal_%m%d_%H%M")
    log = logs / f"{tag}.log"
    cmd = ["uv", "run", "jev", "play", "--logfile", str(log), "--decision-log", str(logs / f"{tag}_decisions.jsonl"),
           "--save-frames", "2", "--frames-dir", f"snapshots/night/{tag}"]
    c, riot = LCU(), RiotLiveClient()
    _say(f"client phase {clear_post_game(c)}")
    if not harness_alive():
        console = open(logs / f"{tag}.console", "a")
        subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
        _say(f"harness: {' '.join(cmd)}")
    started = False
    for line in c.play_normal(first=first, second=second):
        _say(f"lcu: {line}")
        started = started or line == "game starting"
    result = {"tag": tag, "log": str(log), "ended": "?"}
    if not started:
        result["ended"] = "no game"
        stop_harness()
        return result
    t0 = time.time()
    while not game_alive() and time.time() - t0 < 300:
        time.sleep(2)
    gone, last_note = 0, 0.0
    while time.time() - t0 < max_minutes * 60 + 300:
        time.sleep(5)
        if not game_alive():
            gone += 1
            if gone >= 3:
                result["ended"] = "game over"
                break
            continue
        gone = 0
        if not harness_alive():
            _say("harness not running: restarting it")
            console = open(logs / f"{tag}.console", "a")
            subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
        if time.time() - last_note > 60:
            last_note = time.time()
            try:
                _say("status " + log.read_text(errors="ignore").splitlines()[-1][:160])
            except (OSError, IndexError):
                pass
    stop_harness()
    from jev.score import report

    result["score"] = report(log)
    _say("score:\n" + result["score"])
    time.sleep(5)
    _say(f"client phase after: {clear_post_game(c)}")
    c.close()
    return result
