"""`jev` command line."""
from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def doctor() -> None:
    """Check key, League install, game API, macOS permissions, screen capture."""
    from jev.doctor import run_all

    t = Table(title="jev doctor")
    t.add_column("check")
    t.add_column("ok")
    t.add_column("detail")
    for name, ok, msg in run_all():
        t.add_row(Text(name), Text("yes" if ok else "no", style="green" if ok else "red"), Text(msg))
    console.print(t)


@app.command()
def brain(fixture: Path = typer.Argument(Path("tests/fixtures/midgame_yasuo_vs_zed.json")), runs: int = 1) -> None:
    """Ask Jev about a recorded game state and print the decision."""
    from jev.brain import Brain
    from jev.riot_api import load_fixture
    from jev.state import Perception, build_state

    state = build_state(load_fixture(fixture), Perception())
    console.print_json(json.dumps(state))
    b = Brain()
    for _ in range(runs):
        console.print(b.decide(state).summary())


@app.command()
def snapshot(out: Path = Path("snapshots")) -> None:
    """Save a screenshot so the minimap and shop geometry can be calibrated by eye."""
    from jev.screen import Screen

    s = Screen()
    path = s.save(out / f"{time.strftime('%H%M%S')}.png")
    console.print(f"saved {path} ({s.describe()})")


@app.command()
def keys() -> None:
    """Show the key bindings and quick-cast flags read from League's config."""
    from jev import keybinds

    console.print(keybinds.load().describe(), markup=False, highlight=False)
    console.print(str(keybinds.load_game_cfg()), markup=False, highlight=False)


@app.command()
def record(hz: float = 2.0) -> None:
    """Record Live Client API snapshots to fixtures/ while a game runs."""
    from jev.riot_api import FixtureRecorder, RiotLiveClient

    c = RiotLiveClient()
    console.print("waiting for a game...")
    while not c.is_game_running():
        time.sleep(1)
    rec = FixtureRecorder()
    console.print(f"recording to {rec.dir}")
    while True:
        d = c.all_game_data()
        if d is None:
            break
        rec.write(d)
        time.sleep(1 / hz)
    console.print(f"game over, {rec.n} snapshots")


@app.command()
def play(
    dry_run: bool = typer.Option(False, help="Log actions instead of sending input"),
    logfile: Path | None = typer.Option(None, help="Append one status line per second here"),
    keep_front: bool = typer.Option(True, help="Re-activate the game window whenever it is not in front"),
    overlay: bool = typer.Option(True, help="Show the on-screen panel with Jev's decision and probabilities"),
    forever: bool = typer.Option(False, help="After a game ends, wait for the next one"),
    overlay_corner: str = typer.Option("tr", help="Overlay corner: tl, tr, bl, br"),
    tactic_hz: float = typer.Option(8.0, help="Max tactical Jev calls per second while in lane (0 = off)"),
    champion: str = typer.Option("", help="Kit override: yasuo | thresh (default: the champion in the game)"),
    role: str = typer.Option("", help="Role override: MIDDLE | UTILITY | TOP | BOTTOM (default: assigned position)"),
    explore: float = typer.Option(0.0, help="Share of tactical picks sampled from Jev's distribution instead of its top choice (bot games)"),
    decision_log: str = typer.Option("logs/decisions.jsonl", help="Tactical decisions with outcomes, for `jev review`"),
    save_frames: float = typer.Option(0.0, help="Save a raw frame to snapshots/live every N seconds (calibration)"),
    capture: str = typer.Option("sck", help="Screen capture: sck (ScreenCaptureKit stream) | mss (blocking grab)"),
    capture_fps: int = typer.Option(60, help="ScreenCaptureKit frame rate cap; 120 on a 120 Hz display halves the frame wait"),
    markers: bool = typer.Option(True, help="Draw Jev's last move (target ring, aim line) over the game"),
    frames_dir: str = typer.Option("snapshots/live", help="Where --save-frames writes (JPEG; 4/s while an enemy champion is on screen)"),
) -> None:
    """Play the current game with Jev driving strategy (1/s), tactics (~7/s) and the build."""
    from jev.loop import Player

    player = Player(dry_run=dry_run, logfile=logfile, keep_front=keep_front, forever=forever, tactic_hz=tactic_hz,
                    champion=champion or None, role=role, explore=explore, decision_log=decision_log,
                    save_frames_s=save_frames, capture=capture, capture_fps=capture_fps, frames_dir=frames_dir)
    if not overlay:
        player.run()
        return
    from jev.control import game_is_frontmost
    from jev.overlay import run_with_overlay

    run_with_overlay(player.run, player.overlay_data, corner=overlay_corner, markers=markers, game_active=game_is_frontmost)


@app.command()
def overlay_demo(markers: bool = typer.Option(True, help="Draw the marker layer too")) -> None:
    """Show the overlay with a replayed fight (no game needed): move it, resize, try compact mode."""
    from jev.overlay import Overlay
    from jev.overlay_demo import DemoFeed

    Overlay(DemoFeed().snapshot, markers=markers, game_active=lambda: False).run_forever()


@app.command()
def score(path: Path = typer.Argument(Path("/tmp/jev-helper/play.log"))) -> None:
    """Per-game report from a play log: deaths and when, KDA, CS/min, time blind/paused/dead."""
    from jev.score import report

    console.print(report(path), markup=False)


@app.command()
def digest(play_log: Path = typer.Option(Path("logs/play.log")), decisions: Path = typer.Option(Path("logs/decisions.jsonl")),
           since: float = typer.Option(90.0, help="Seconds to look back")) -> None:
    """Last N seconds of play: deaths, moves executed, hits landed, last hits, paused/blind share."""
    from jev.digest import digest as run

    console.print(run(play_log, decisions, since), markup=False)


@app.command()
def review(path: Path = typer.Argument(Path("logs/decisions.jsonl"))) -> None:
    """Summarise logged tactical decisions: outcome per action, menu gaps, near-ties."""
    from jev.decisions import review as run_review

    console.print(run_review(path), markup=False)


@app.command()
def lcu(action: str = typer.Argument("status", help="status | practice | custom | bots | start | pick | normal | queues"),
        champion: str = typer.Option("yasuo", help="For pick: yasuo | thresh")) -> None:
    """Drive the League client: lobbies, queue, ready check, champ select."""
    from jev.lcu import CHAMPIONS, LCU, YASUO

    c = LCU()
    if action == "normal":
        for line in c.play_normal():
            console.print(line, markup=False)
    elif action == "botgame":
        pos = {"leesin": "jungle", "thresh": "utility"}.get(champion.lower(), "middle")
        for line in c.bot_game(CHAMPIONS.get(champion.lower(), YASUO), position=pos):
            console.print(line, markup=False)
    elif action == "queues":
        for q in c.available_queues():
            console.print(q, markup=False)
    elif action == "status":
        console.print(f"lockfile {c.lockfile}")
        console.print(f"phase: {c.gameflow()}")
        console.print(c.summoner())
    elif action == "practice":
        console.print(c.create_practice_tool())
    elif action == "custom":
        console.print(c.create_custom_vs_bots())
    elif action == "bots":
        console.print(c.add_bot(238, "200"))   # Zed mid
    elif action == "start":
        console.print(c.start_champ_select())
    elif action == "pick":
        console.print(c.pick(CHAMPIONS.get(champion.lower(), YASUO)))
    c.close()


@app.command()
def session(champion: str = typer.Option("yasuo", help="yasuo | leesin | thresh"),
            minutes: float = typer.Option(18.0, help="Leave the game (surrender) at this game time"),
            tag: str = typer.Option("", help="Name for the log and frames (default: date_champion)"),
            difficulty: str = typer.Option("RSINTERMEDIATE", help="Bot difficulty"),
            explore: float = typer.Option(0.0, help="Passed to jev play")) -> None:
    """One unattended bot game end to end: lobby, pick, the harness playing, time limit, score."""
    from jev.session import run

    extra = ["--explore", str(explore)] if explore else []
    run(champion, minutes, tag, difficulty, extra)


@app.command()
def night(rotation: str = typer.Option("yasuo,leesin", help="Champions in turn (logs/night/rotation.txt overrides, re-read each game)"),
          minutes: float = typer.Option(16.0, help="Surrender at this game time"),
          games: int = typer.Option(30, help="Stop after this many games (or when logs/night/STOP appears)")) -> None:
    """Back-to-back unattended bot games for the overnight improvement loop."""
    from jev.session import night as run_night

    run_night(rotation, minutes, games)


@app.command()
def watch_install(interval: float = 30.0) -> None:
    """Block until League of Legends.app appears in /Applications."""
    from jev.doctor import LEAGUE_APP

    while not LEAGUE_APP.exists():
        console.print(f"{time.strftime('%H:%M:%S')} not installed yet")
        time.sleep(interval)
    console.print(f"installed: {LEAGUE_APP}")


if __name__ == "__main__":
    app()
