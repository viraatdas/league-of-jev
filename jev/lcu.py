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
THRESH = 412
LEE_SIN = 64
CHAMPIONS = {"yasuo": YASUO, "thresh": THRESH, "leesin": LEE_SIN}
# Assigned position -> champions to try in order.
PICKS_BY_POSITION = {"middle": [YASUO, LEE_SIN], "jungle": [LEE_SIN, YASUO], "utility": [THRESH, YASUO]}
DEFAULT_PICKS = [YASUO, LEE_SIN]
BANS = [238, 91, 7]  # Zed, Talon, LeBlanc: first one not already banned or hovered
# Summoner spells per position (ids): Flash 4, Ignite 14, Smite 11, Exhaust 3, Heal 7, Teleport 12.
SPELLS_BY_POSITION = {"middle": (4, 14), "jungle": (4, 11), "utility": (4, 3), "bottom": (4, 7), "top": (4, 12), "": (4, 14)}


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

    def add_bot(self, champion_id: int, team: str = "200", difficulty: str = "RSINTERMEDIATE", position: str = "") -> tuple[int, Any]:
        import uuid

        return self.req("POST", "/lol-lobby/v1/lobby/custom/bots",
                        {"botDifficulty": difficulty, "championId": champion_id, "teamId": team,
                         "position": position, "botUuid": str(uuid.uuid4())})

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

    SELECTION_PATHS = ("/lol-champ-select/v1/session/my-selection",
                       "/lol-lobby-team-builder/champ-select/v1/session/my-selection",
                       "/lol-champ-select-legacy/v1/session/my-selection")

    def my_spells(self) -> tuple[int | None, int | None]:
        c2, sess = self.req("GET", "/lol-champ-select/v1/session")
        if c2 != 200 or not isinstance(sess, dict):
            return None, None
        cell = sess.get("localPlayerCellId")
        me = next((p for p in sess.get("myTeam", []) if p.get("cellId") == cell), {})
        return me.get("spell1Id"), me.get("spell2Id")

    def set_spell_ids(self, s1: int, s2: int, tries: int = 2) -> tuple[bool, str]:
        """PATCH each my-selection endpoint in turn until the session reads back the spells.
        Returns (ok, what happened) for the log."""
        notes = []
        for _ in range(tries):
            for path in self.SELECTION_PATHS:
                code, body = self.req("PATCH", path, {"spell1Id": s1, "spell2Id": s2})
                time.sleep(0.7)
                got = self.my_spells()
                notes.append(f"{path.split('/')[1]}:{code}->{got}")
                if set(got) == {s1, s2}:
                    return True, " ".join(notes)
        return False, " ".join(notes)

    def set_spells(self, position: str, tries: int = 2) -> tuple[int, Any]:
        """Set the summoner spells for the position and read them back (a 204 alone did not mean
        they stuck: a Lee Sin jungle game started with Flash + Ignite)."""
        s1, s2 = SPELLS_BY_POSITION.get((position or "").lower(), SPELLS_BY_POSITION[""])
        ok, notes = self.set_spell_ids(s1, s2, tries)
        return (204 if ok else 0), ({"spells": [s1, s2], "how": notes[-160:]} if ok else {"spells_not_confirmed": [s1, s2], "tried": notes})

    def probe_spells(self) -> str:
        """Diagnostic: switch to Flash + Smite and back, reporting what each endpoint did."""
        before = self.my_spells()
        ok1, n1 = self.set_spell_ids(4, 11, tries=1)
        ok2, n2 = self.set_spell_ids(*(before if None not in before else (4, 14)), tries=1)
        return f"probe before={before} to-smite ok={ok1} [{n1}] back ok={ok2} [{n2}]"

    def available_bots(self) -> list[dict]:
        code, bots = self.req("GET", "/lol-lobby/v2/lobby/custom/available-bots")
        return bots if code == 200 and isinstance(bots, list) else []

    def bot_game(self, champion_id: int = YASUO, difficulty: str = "RSINTERMEDIATE", timeout_s: float = 240.0,
                 position: str = "middle"):
        """Custom 5v5 on Summoner's Rift: me plus four allied bots against five bots. Picks the
        champion, sets the summoner spells, and yields progress lines until the game starts."""
        self.delete_lobby()
        time.sleep(1)
        code, body = self.create_custom_vs_bots()
        yield f"custom lobby: {code} {body if code >= 400 else ''}"
        if code >= 400:
            return
        bots = [b.get("id") for b in self.available_bots() if b.get("id") and b.get("id") != champion_id]
        yield f"{len(bots)} bot champions available"
        allies = [p for p in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY") if p != position.upper()][:4]
        slots = [("100", p) for p in allies] + [("200", p) for p in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")]
        for (team, pos), cid in zip(slots, bots):
            c, b = self.add_bot(cid, team, difficulty, pos)
            yield f"bot {cid} {team} {pos}: {c} {b if c >= 400 else ''}"
        c, b = self.start_champ_select()
        yield f"start champ select: {c} {b if c >= 400 else ''}"
        t0 = time.time()
        picked = False
        tried_early = False
        while time.time() - t0 < timeout_s:
            phase = self.gameflow()
            if phase == "ChampSelect" and not picked:
                code, sess = self.req("GET", "/lol-champ-select/v1/session")
                if code == 200:
                    if not tried_early:
                        # Spells at three moments (before the hover, after it, after the lock): a
                        # 204 alone never meant they stuck, and which moment works is not known.
                        tried_early = True
                        import subprocess
                        subprocess.run(["screencapture", "-x", "logs/night/champselect.png"], check=False)
                        ok, how = self.set_spell_ids(*SPELLS_BY_POSITION.get(position, SPELLS_BY_POSITION[""]), tries=1)
                        yield f"spells before hover: ok={ok} [{how}] phase={(sess.get('timer') or {}).get('phase')}"
                    cell = sess.get("localPlayerCellId")
                    for g in sess.get("actions", []):
                        for a in g:
                            if a.get("actorCellId") == cell and a.get("type") == "pick" and not a.get("completed"):
                                self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id})
                                c2, b2 = self.set_spells(position)
                                yield f"spells after hover: {c2} {b2}"
                                c1, b1 = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": champion_id, "completed": True})
                                yield f"pick {champion_id}: {c1} {b1 if c1 >= 400 else ''}"
                                picked = c1 < 400
                                if picked:
                                    want = set(SPELLS_BY_POSITION.get(position, SPELLS_BY_POSITION[""]))
                                    for _ in range(4):
                                        if set(self.my_spells()) == want:
                                            break
                                        ok, how = self.set_spell_ids(*sorted(want, key=lambda v: v != 4), tries=1)
                                        _, s2 = self.req("GET", "/lol-champ-select/v1/session")
                                        yield f"spells after lock: ok={ok} [{how}] phase={((s2 or {}).get('timer') or {}).get('phase')}"
                                        time.sleep(1.0)
            elif phase in ("InProgress", "GameStart"):
                yield "game starting"
                return
            time.sleep(1.5)
        yield "timed out waiting for the game"

    def pickable(self) -> set[int]:
        code, ids = self.req("GET", "/lol-champ-select/v1/pickable-champion-ids")
        return set(ids) if code == 200 and isinstance(ids, list) else set()

    def play_normal(self, picks: dict[str, list[int]] | None = None, timeout_s: float = 900.0,
                    first: str = "MIDDLE", second: str = "JUNGLE"):
        """Create a normal lobby (mid first, support second), queue, accept the ready check, ban,
        and pick by assigned position: mid -> Yasuo, support -> Thresh, each falling back to the
        other if banned, taken, or not owned. Yields progress lines until the game starts."""
        picks = picks or PICKS_BY_POSITION
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
        code, body = self.req("PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences",
                              {"firstPreference": first, "secondPreference": second})
        yield f"positions {first.lower()}/{second.lower()}: {code}"
        code, body = self.req("POST", "/lol-lobby/v2/lobby/matchmaking/search")
        yield f"search: {code} {body if code >= 400 else ''}"
        t0 = time.time()
        picked = False
        tried: dict[int, int] = {}  # action id -> attempts (never hammer one action)
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
                    for line in self._champ_select_step(sess, picks, tried):
                        if line.startswith("picked"):
                            picked = True
                        yield line
            elif phase in ("InProgress", "GameStart"):
                yield "game starting"
                return
            elif phase in ("None", "Lobby") and time.time() - t0 > 20 and last in ("ChampSelect",):
                yield "champ select ended without a game (dodge?)"
                return
            time.sleep(1.5)
        yield "timed out waiting for a game"

    def _champ_select_step(self, sess: dict, picks: dict[str, list[int]], tried: dict[int, int]):
        cell = sess.get("localPlayerCellId")
        me = next((p for p in sess.get("myTeam", []) if p.get("cellId") == cell), {})
        position = str(me.get("assignedPosition") or "").lower()
        order = picks.get(position, DEFAULT_PICKS)
        bans = {a.get("championId") for g in sess.get("actions", []) for a in g if a.get("type") == "ban" and a.get("completed")}
        taken = {p.get("championId") for p in sess.get("myTeam", []) + sess.get("theirTeam", []) if p.get("cellId") != cell}
        hovered = {p.get("championPickIntent") for p in sess.get("myTeam", []) if p.get("cellId") != cell}
        pickable = self.pickable()
        options = [c for c in order if c not in bans and c not in taken and (not pickable or c in pickable)]
        for group in sess.get("actions", []):
            for a in group:
                if a.get("actorCellId") != cell or a.get("completed") or not a.get("isInProgress"):
                    continue
                if tried.get(a["id"], 0) >= 3:
                    continue
                tried[a["id"]] = tried.get(a["id"], 0) + 1
                if a.get("type") == "ban":
                    ban = next((b for b in BANS if b not in bans and b not in hovered and b not in order), None)
                    if ban is not None:
                        c1, b1 = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": ban, "completed": True})
                        yield f"ban {ban}: {c1} {b1 if c1 >= 400 else ''}"
                elif a.get("type") == "pick":
                    for cid in options:
                        # Hover, set spells, then lock (spells set after the lock were ignored).
                        self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": cid})
                        c2, b2 = self.set_spells(position, tries=3)
                        c1, b1 = self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": cid, "completed": True})
                        yield f"pick {cid} for {position or 'no position'}: {c1} {b1 if c1 >= 400 else ''}; spells {b2}"
                        if c1 < 400:
                            yield f"picked {cid}"
                            return
                    if not options:
                        yield f"none of {order} available for {position or 'no position'}"
        # Hover the first option early so teammates see it.
        for group in sess.get("actions", []):
            for a in group:
                if (a.get("actorCellId") == cell and a.get("type") == "pick" and not a.get("completed")
                        and a.get("championId", 0) == 0 and options):
                    self.req("PATCH", f"/lol-champ-select/v1/session/actions/{a['id']}", {"championId": options[0]})

    def close(self) -> None:
        self.http.close()
