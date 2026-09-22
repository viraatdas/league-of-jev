"""`jev` command line."""
from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
        t.add_row(name, "[green]yes" if ok else "[red]no", msg)
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
) -> None:
    """Play the current game as Yasuo with Jev driving intent."""
    from jev.loop import Player

    Player(dry_run=dry_run, logfile=logfile).run()


@app.command()
def lcu(action: str = typer.Argument("status", help="status | practice | custom | bots | start | pick")) -> None:
    """Drive the League client: create a Practice Tool lobby, add bots, start, pick Yasuo."""
    from jev.lcu import LCU, YASUO

    c = LCU()
    if action == "status":
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
        console.print(c.pick(YASUO))
    c.close()


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
