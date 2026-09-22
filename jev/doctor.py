"""Environment checks so the first real run has no surprises."""
from __future__ import annotations

import json
import os
from pathlib import Path

LEAGUE_APP = Path("/Applications/League of Legends.app")
RIOT_CLIENT = Path("/Users/Shared/Riot Games/Riot Client.app")
RIOT_INSTALLS = Path("/Users/Shared/Riot Games/RiotClientInstalls.json")


def check_key() -> tuple[bool, str]:
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("TYPESAFE_API_KEY"):
        return False, "TYPESAFE_API_KEY missing from .env"
    try:
        from typesafe_sdk import TypeSafeClient

        with TypeSafeClient() as c:
            names = [m.name for m in c.models.list().models]
        return True, f"key valid, models: {', '.join(names)}"
    except Exception as e:  # noqa: BLE001
        return False, f"TypeSafe call failed: {e}"


def check_league() -> tuple[bool, str]:
    if LEAGUE_APP.exists():
        return True, f"installed at {LEAGUE_APP}"
    note = "not installed yet"
    if RIOT_CLIENT.exists():
        note += "; Riot Client present"
    if RIOT_INSTALLS.exists():
        try:
            d = json.loads(RIOT_INSTALLS.read_text())
            note += f"; installs.json: {json.dumps(d)[:160]}"
        except ValueError:
            pass
    return False, note


def check_game_api() -> tuple[bool, str]:
    from jev.riot_api import RiotLiveClient

    c = RiotLiveClient()
    try:
        gs = c.game_stats()
    finally:
        c.close()
    if gs is None:
        return False, "no game running (API on 127.0.0.1:2999 not answering)"
    return True, f"game running: {gs.get('gameMode')} t={gs.get('gameTime', 0):.0f}s"


def check_accessibility() -> tuple[bool, str]:
    try:
        from ApplicationServices import AXIsProcessTrusted

        ok = bool(AXIsProcessTrusted())
    except Exception as e:  # noqa: BLE001
        return False, f"could not query Accessibility: {e}"
    return ok, "trusted" if ok else "NOT trusted: System Settings > Privacy & Security > Accessibility > enable your terminal (Ghostty)"


def check_screen_recording() -> tuple[bool, str]:
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        ok = bool(CGPreflightScreenCaptureAccess())
    except Exception as e:  # noqa: BLE001
        return False, f"could not query Screen Recording: {e}"
    return ok, "granted" if ok else "NOT granted: System Settings > Privacy & Security > Screen Recording > enable your terminal (Ghostty)"


def check_capture() -> tuple[bool, str]:
    try:
        from jev.screen import Screen

        s = Screen()
        f = s.grab()
        return True, f"{s.describe()}, frame mean brightness {float(f.mean()):.0f}"
    except Exception as e:  # noqa: BLE001
        return False, f"capture failed: {e}"


def check_keybinds() -> tuple[bool, str]:
    from jev import keybinds

    kb = keybinds.load()
    return kb.from_config, kb.describe()


def check_game_files() -> tuple[bool, str]:
    """Layout-agnostic: look for the game binary anywhere in the bundle and report size."""
    if not LEAGUE_APP.exists():
        return False, "bundle missing"
    binaries = [p for p in LEAGUE_APP.rglob("LeagueofLegends") if p.is_file() and "MacOS" in p.parts]
    total = 0
    for p in LEAGUE_APP.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    gb = total / 1e9
    patching = Path("/Users/Shared/Riot Games/Metadata/league_of_legends.live.game_patch").exists()
    if not binaries:
        return False, f"no game binary yet ({gb:.1f} GB on disk, patch dir {'present' if patching else 'absent'})"
    return True, f"game binary at {binaries[0].relative_to(LEAGUE_APP)} ({gb:.1f} GB on disk)"


def check_frontmost() -> tuple[bool, str]:
    from jev.control import frontmost_app_name

    name = frontmost_app_name()
    return "league" in name.lower(), f"frontmost app: {name or 'unknown'} (input only sent while League is frontmost)"


def run_all() -> list[tuple[str, bool, str]]:
    checks = [
        ("TypeSafe key", check_key),
        ("League installed", check_league),
        ("Game files", check_game_files),
        ("Keybinds", check_keybinds),
        ("Frontmost app", check_frontmost),
        ("Game API", check_game_api),
        ("Accessibility", check_accessibility),
        ("Screen Recording", check_screen_recording),
        ("Screen capture", check_capture),
    ]
    out = []
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"crashed: {e}"
        out.append((name, ok, msg))
    return out
