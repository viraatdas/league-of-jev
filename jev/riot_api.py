"""Riot Live Client Data API: the game serves this on localhost while a match runs."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://127.0.0.1:2999"


class RiotLiveClient:
    """Thin poller over the local game API. The cert is self-signed, so verify=False for this host only."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 0.6) -> None:
        self._http = httpx.Client(base_url=base_url, verify=False, timeout=timeout)

    def _get(self, path: str) -> Any | None:
        try:
            r = self._http.get(path)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def game_stats(self) -> dict | None:
        return self._get("/liveclientdata/gamestats")

    def is_game_running(self) -> bool:
        return self.game_stats() is not None

    def all_game_data(self) -> dict | None:
        return self._get("/liveclientdata/allgamedata")

    def event_data(self) -> dict | None:
        return self._get("/liveclientdata/eventdata")

    def close(self) -> None:
        self._http.close()


class FixtureRecorder:
    """Writes every snapshot to fixtures/<session>/<tick>.json so games can be replayed offline."""

    def __init__(self, root: Path = Path("fixtures")) -> None:
        self.dir = root / time.strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def write(self, data: dict, extra: dict | None = None) -> Path:
        payload = {"t": time.time(), "data": data, "extra": extra or {}}
        path = self.dir / f"{self.n:06d}.json"
        path.write_text(json.dumps(payload))
        self.n += 1
        return path


def load_fixture(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text())
    return raw["data"] if "data" in raw and "activePlayer" in raw.get("data", {}) else raw
