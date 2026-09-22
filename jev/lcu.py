"""League Client (LCU) helper: create a Practice Tool lobby and pick a champion through the
client's local REST API. Riot documents this API as unsupported for third parties; it is the
same interface the client's own UI uses."""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx

LOCKFILE_CANDIDATES = [
    Path("/Applications/League of Legends.app/Contents/LoL/lockfile"),
    Path("/Applications/League of Legends.app/Contents/LoL/LeagueClient.app/Contents/lockfile"),
    Path.home() / "Library/Application Support/Riot Games/League of Legends/lockfile",
]
YASUO = 157


def find_lockfile() -> Path | None:
    for p in LOCKFILE_CANDIDATES:
        if p.exists():
            return p
    root = Path("/Applications/League of Legends.app")
    for p in root.rglob("lockfile"):
        return p
    return None


class LCU:
    def __init__(self) -> None:
        lf = find_lockfile()
        if lf is None:
            raise RuntimeError("League client lockfile not found; is the client running?")
        name, pid, port, password, proto = lf.read_text().strip().split(":")
        token = base64.b64encode(f"riot:{password}".encode()).decode()
        self.http = httpx.Client(
            base_url=f"{proto}://127.0.0.1:{port}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            verify=False,
            timeout=5.0,
        )
        self.lockfile = lf

    def req(self, method: str, path: str, json: Any | None = None) -> tuple[int, Any]:
        r = self.http.request(method, path, json=json)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body

    # -- state ---------------------------------------------------------------------------
    def summoner(self) -> Any:
        return self.req("GET", "/lol-summoner/v1/current-summoner")[1]

    def gameflow(self) -> str:
        code, body = self.req("GET", "/lol-gameflow/v1/gameflow-phase")
        return str(body).strip('"') if code == 200 else f"http {code}"

    # -- lobby -----------------------------------------------------------------------------
    PRACTICE_TOOL_QUEUE = 3140
    CUSTOM_BLIND_QUEUE = 3100

    def delete_lobby(self) -> tuple[int, Any]:
        return self.req("DELETE", "/lol-lobby/v2/lobby")

    def lobby(self) -> tuple[int, Any]:
        return self.req("GET", "/lol-lobby/v2/lobby")

    def create_practice_tool(self) -> tuple[int, Any]:
        """The custom-lobby payload returns INVALID_LOBBY on this client; the queue form works."""
        return self.req("POST", "/lol-lobby/v2/lobby", {"queueId": self.PRACTICE_TOOL_QUEUE})

    def create_practice_tool_legacy(self) -> tuple[int, Any]:
        payload = {
            "customGameLobby": {
                "configuration": {
                    "gameMode": "PRACTICETOOL",
                    "gameMutator": "",
                    "gameServerRegion": "",
                    "mapId": 11,
                    "mutators": {"id": 1},
                    "spectatorPolicy": "AllAllowed",
                    "teamSize": 5,
                },
                "lobbyName": "jev practice",
                "lobbyPassword": "",
            },
            "isCustom": True,
        }
        return self.req("POST", "/lol-lobby/v2/lobby", payload)

    def create_custom_vs_bots(self) -> tuple[int, Any]:
        return self.req("POST", "/lol-lobby/v2/lobby", {"queueId": self.CUSTOM_BLIND_QUEUE})

    def create_custom_vs_bots_legacy(self) -> tuple[int, Any]:
        payload = {
            "customGameLobby": {
                "configuration": {
                    "gameMode": "CLASSIC",
                    "gameMutator": "",
                    "gameServerRegion": "",
                    "mapId": 11,
                    "mutators": {"id": 1},
                    "spectatorPolicy": "AllAllowed",
                    "teamSize": 5,
                },
                "lobbyName": "jev custom",
                "lobbyPassword": "",
            },
            "isCustom": True,
        }
        return self.req("POST", "/lol-lobby/v2/lobby", payload)

    def add_bot(self, champion_id: int, team: str = "200", difficulty: str = "MEDIUM") -> tuple[int, Any]:
        return self.req("POST", "/lol-lobby/v1/lobby/custom/bots", {"botDifficulty": difficulty, "championId": champion_id, "teamId": team})

    def start_champ_select(self) -> tuple[int, Any]:
        return self.req("POST", "/lol-lobby/v1/lobby/custom/start-champ-select")

    # -- champ select --------------------------------------------------------------------
    def pick(self, champion_id: int = YASUO, timeout_s: float = 60.0) -> str:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            code, sess = self.req("GET", "/lol-champ-select/v1/session")
            if code != 200:
                time.sleep(1)
                continue
            cell = sess.get("localPlayerCellId")
            for group in sess.get("actions", []):
                for a in group:
                    if a.get("actorCellId") == cell and a.get("type") == "pick" and not a.get("completed"):
                        c1, _ = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id})
                        c2, b2 = self.req("POST", f"/lol-champ-select/v1/session/actions/{a['id']}/complete")
                        return f"pick patch {c1}, complete {c2} {b2 if c2 >= 400 else ''}"
            time.sleep(1)
        return "no pick action found"

    def close(self) -> None:
        self.http.close()
