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

    def _custom(self, queue_id: int, game_mode: str, name: str) -> dict:
        # This client wants both the custom queue id and the customGameLobby block.
        return {
            "queueId": queue_id,
            "customGameLobby": {
                "configuration": {
                    "gameMode": game_mode,
                    "gameMutator": "",
                    "gameServerRegion": "",
                    "mapId": 11,
                    "mutators": {"id": 1},
                    "spectatorPolicy": "AllAllowed",
                    "teamSize": 5,
                },
                "lobbyName": name,
                "lobbyPassword": None,
            },
            "isCustom": True,
        }

    def create_practice_tool(self) -> tuple[int, Any]:
        return self.req("POST", "/lol-lobby/v2/lobby", self._custom(self.PRACTICE_TOOL_QUEUE, "PRACTICETOOL", "jev practice"))

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
        return self.req("POST", "/lol-lobby/v2/lobby", self._custom(self.CUSTOM_BLIND_QUEUE, "CLASSIC", "jev custom"))

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
                        c1, b1 = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id, "completed": True})
                        if c1 >= 400:
                            c1b, _ = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id})
                            c2, b2 = self.req("POST", f"/lol-champ-select/v1/session/actions/{a['id']}/complete")
                            return f"lock via patch failed {c1} {b1}; hover {c1b}, complete {c2} {b2 if c2 >= 400 else ''}"
                        return f"locked via patch completed=true ({c1})"
            time.sleep(1)
        return "no pick action found"

    # -- matchmade games -----------------------------------------------------------------
    def available_queues(self) -> list[str]:
        code, qs = self.req("GET", "/lol-game-queues/v1/queues")
        out = []
        if code == 200:
            for q in qs:
                if q.get("queueAvailability") == "Available" and q.get("mapId") == 11:
                    out.append(f"{q.get('id')} {q.get('category')} {q.get('gameMode')} {q.get('name')} {q.get('description', '')[:40]}")
        return out

    def normal_queue_id(self) -> int | None:
        code, qs = self.req("GET", "/lol-game-queues/v1/queues")
        if code != 200:
            return None
        avail = [q for q in qs if q.get("queueAvailability") == "Available" and q.get("mapId") == 11 and q.get("category") == "PvP"]
        for want in ("draft", "normal", "blind", "quickplay", "swiftplay"):
            for q in avail:
                if want in str(q.get("name", "")).lower() and "ranked" not in str(q.get("name", "")).lower():
                    return int(q["id"])
        return None

    def play_normal(self, champion_id: int, fallbacks: tuple[int, ...] = (777, 86), timeout_s: float = 900.0):
        """Create a normal lobby, prefer mid, queue, accept the ready check, ban and pick, and
        yield progress lines until the game is in progress."""
        qid = self.normal_queue_id()
        if qid is None:
            yield "no available normal queue found"
            return
        self.delete_lobby()
        time.sleep(1)
        code, body = self.req("POST", "/lol-lobby/v2/lobby", {"queueId": qid})
        yield f"lobby queue {qid}: {code} {body.get('gameConfig', {}).get('queueId') if code < 400 else body}"
        if code >= 400:
            return
        code, body = self.req("PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences", {"firstPreference": "MIDDLE", "secondPreference": "TOP"})
        yield f"positions mid/top: {code}"
        code, body = self.req("POST", "/lol-lobby/v2/lobby/matchmaking/search")
        yield f"search: {code} {body if code >= 400 else ''}"
        t0 = time.time()
        picked = False
        last = ""
        while time.time() - t0 < timeout_s:
            phase = self.gameflow()
            if phase != last:
                yield f"{int(time.time() - t0)}s phase {phase}"
                last = phase
            if phase == "ReadyCheck":
                code, rc = self.req("GET", "/lol-matchmaking/v1/ready-check")
                if code == 200 and rc.get("state") == "InProgress" and rc.get("playerResponse") != "Accepted":
                    c2, _ = self.req("POST", "/lol-matchmaking/v1/ready-check/accept")
                    yield f"accepted ready check: {c2}"
            elif phase == "ChampSelect":
                code, sess = self.req("GET", "/lol-champ-select/v1/session")
                if code == 200:
                    cell = sess.get("localPlayerCellId")
                    bans = {a.get("championId") for g in sess.get("actions", []) for a in g if a.get("type") == "ban" and a.get("completed")}
                    taken = {p.get("championId") for p in sess.get("myTeam", []) + sess.get("theirTeam", []) if p.get("cellId") != cell}
                    for group in sess.get("actions", []):
                        for a in group:
                            if a.get("actorCellId") != cell or a.get("completed") or not a.get("isInProgress"):
                                continue
                            if a.get("type") == "ban":
                                c1, _ = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": 238, "completed": True})
                                yield f"banned Zed: {c1}"
                            elif a.get("type") == "pick" and not picked:
                                for cid in (champion_id,) + fallbacks:
                                    if cid in bans or cid in taken:
                                        continue
                                    c1, b1 = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": cid, "completed": True})
                                    yield f"pick {cid}: {c1} {b1 if c1 >= 400 else ''}"
                                    if c1 < 400:
                                        picked = True
                                        break
                    # Hover intent early so teammates see it.
                    if not picked:
                        for group in sess.get("actions", []):
                            for a in group:
                                if a.get("actorCellId") == cell and a.get("type") == "pick" and not a.get("completed") and a.get("championId", 0) == 0:
                                    self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id})
            elif phase in ("InProgress", "GameStart"):
                yield "game starting"
                return
            elif phase in ("None", "Lobby") and time.time() - t0 > 20 and last in ("ChampSelect",):
                yield "champ select ended without a game (dodge?)"
                return
            time.sleep(1.5)
        yield "timed out waiting for a game"

    def close(self) -> None:
        self.http.close()
