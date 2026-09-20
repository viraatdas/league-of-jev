"""Read League's own key bindings and quick-cast flags so the bot presses what the user configured.

League writes Config/input.ini (and PersistedSettings.json) after the first game. Until then
this falls back to the documented defaults and says so in `jev doctor`.
"""
from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CANDIDATE_DIRS = [
    Path("/Applications/League of Legends.app/Contents/LoL/Config"),
    Path.home() / "Library/Application Support/Riot Games/League of Legends/Config",
    Path("/Users/Shared/Riot Games/League of Legends/Config"),
]

DEFAULT_EVENTS: dict[str, str] = {
    "evtCastSpell1": "[q]",
    "evtCastSpell2": "[w]",
    "evtCastSpell3": "[e]",
    "evtCastSpell4": "[r]",
    "evtCastAvatarSpell1": "[d]",
    "evtCastAvatarSpell2": "[f]",
    "evtLevelSpell1": "[Ctrl][q]",
    "evtLevelSpell2": "[Ctrl][w]",
    "evtLevelSpell3": "[Ctrl][e]",
    "evtLevelSpell4": "[Ctrl][r]",
    "evtPlayerAttackMoveClick": "[a]",
    "evtPlayerStopPosition": "[s]",
    "evtUseItem1": "[1]",
    "evtUseItem2": "[2]",
    "evtUseItem3": "[3]",
    "evtUseItem4": "[5]",
    "evtUseItem5": "[6]",
    "evtUseItem6": "[7]",
    "evtUseItem7": "[b]",
    "evtOpenShop": "[p]",
    "evtCameraLockToggle": "[y]",
    "evtShowScoreBoard": "[Tab]",
}

_KEY_ALIASES = {
    "space": "space", "tab": "tab", "enter": "return", "return": "return", "esc": "escape",
    "escape": "escape", "backspace": "delete", "delete": "delete", "semicolon": ";", "comma": ",",
    "period": ".", "slash": "/", "minus": "-", "equals": "=", "apostrophe": "'", "grave": "`",
}
_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt", "cmd": "cmd", "command": "cmd"}


@dataclass(frozen=True)
class Bind:
    key: str | None = None
    ctrl: bool = False
    shift: bool = False
    alt: bool = False
    cmd: bool = False
    mouse_button: int | None = None

    @classmethod
    def parse(cls, text: str) -> "Bind":
        tokens = re.findall(r"\[([^\]]+)\]", text or "")
        mods: dict[str, bool] = {}
        key: str | None = None
        mouse: int | None = None
        for tok in tokens:
            t = tok.strip()
            tl = t.lower()
            if tl in _MODIFIERS:
                mods[_MODIFIERS[tl]] = True
            elif tl.startswith("button "):
                try:
                    mouse = int(tl.split()[1])
                except ValueError:
                    mouse = None
            else:
                key = _KEY_ALIASES.get(tl, tl)
        return cls(key=key, mouse_button=mouse, **mods)

    @property
    def usable(self) -> bool:
        return self.key is not None and self.mouse_button is None

    def __str__(self) -> str:
        parts = [m for m, on in (("ctrl", self.ctrl), ("shift", self.shift), ("alt", self.alt), ("cmd", self.cmd)) if on]
        if self.mouse_button is not None:
            parts.append(f"mouse{self.mouse_button}")
        if self.key:
            parts.append(self.key)
        return "+".join(parts) or "(unbound)"


@dataclass
class Keybinds:
    events: dict[str, Bind]
    quickcast: dict[str, int] = field(default_factory=dict)
    source: str = "defaults"
    from_config: bool = False

    def _ev(self, name: str) -> Bind:
        b = self.events.get(name)
        if b is None or not b.usable:
            b = Bind.parse(DEFAULT_EVENTS[name])
        return b

    def ability(self, i: int) -> Bind:  # 1..4 = Q W E R
        return self._ev(f"evtCastSpell{i}")

    def summoner(self, i: int) -> Bind:
        return self._ev(f"evtCastAvatarSpell{i}")

    def level(self, i: int) -> Bind:
        return self._ev(f"evtLevelSpell{i}")

    @property
    def attack_move(self) -> Bind:
        return self._ev("evtPlayerAttackMoveClick")

    @property
    def stop(self) -> Bind:
        return self._ev("evtPlayerStopPosition")

    @property
    def recall(self) -> Bind:
        return self._ev("evtUseItem7")

    @property
    def shop(self) -> Bind:
        return self._ev("evtOpenShop")

    @property
    def camera_lock(self) -> Bind:
        return self._ev("evtCameraLockToggle")

    def quick_cast(self, i: int) -> bool | None:
        """True/False from League's Quickcast section, None when unknown (config not written yet)."""
        for k in (f"evtCastSpell{i}smart", f"evtCastSpell{i}Smart"):
            if k in self.quickcast:
                return self.quickcast[k] >= 1
        return None

    def describe(self) -> str:
        qc = [self.quick_cast(i) for i in (1, 2, 3, 4)]
        qcs = "unknown" if all(q is None for q in qc) else "".join("Y" if q else "n" for q in qc)
        return (
            f"Q/W/E/R={self.ability(1)}/{self.ability(2)}/{self.ability(3)}/{self.ability(4)} "
            f"D/F={self.summoner(1)}/{self.summoner(2)} level={self.level(1)} attack-move={self.attack_move} "
            f"recall={self.recall} shop={self.shop} camlock={self.camera_lock} quickcast(QWER)={qcs} [{self.source}]"
        )


def _parse_input_ini(path: Path) -> tuple[dict[str, Bind], dict[str, int]]:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment]
    cp.read(path, encoding="utf-8-sig")
    events: dict[str, Bind] = {}
    quick: dict[str, int] = {}
    for section in cp.sections():
        for k, v in cp.items(section):
            if section.lower() == "quickcast":
                try:
                    quick[k] = int(float(v))
                except ValueError:
                    pass
            elif k.startswith("evt"):
                events[k] = Bind.parse(v)
    return events, quick


def _parse_persisted(path: Path) -> tuple[dict[str, Bind], dict[str, int]]:
    events: dict[str, Bind] = {}
    quick: dict[str, int] = {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    for f in data.get("files", []):
        if f.get("name", "").lower() != "input.ini":
            continue
        for sec in f.get("sections", []):
            for s in sec.get("settings", []):
                k, v = s.get("name", ""), s.get("value", "")
                if sec.get("name", "").lower() == "quickcast":
                    try:
                        quick[k] = int(float(v))
                    except ValueError:
                        pass
                elif k.startswith("evt"):
                    events[k] = Bind.parse(str(v))
    return events, quick


def find_config_dir() -> Path | None:
    for d in CANDIDATE_DIRS:
        if d.is_dir():
            return d
    roots = [Path("/Applications/League of Legends.app"), Path.home() / "Library/Application Support/Riot Games"]
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("Contents/*/Config", "Contents/*/*/Config", "*/Config", "*/*/Config"):
            for p in root.glob(pattern):
                if p.is_dir() and ((p / "input.ini").exists() or (p / "PersistedSettings.json").exists() or (p / "game.cfg").exists()):
                    return p
    return None


def load(config_dir: Path | None = None) -> Keybinds:
    events = {k: Bind.parse(v) for k, v in DEFAULT_EVENTS.items()}
    quick: dict[str, int] = {}
    d = config_dir or find_config_dir()
    if d is None:
        return Keybinds(events, quick, "defaults: no League Config dir yet (play one game so League writes input.ini)")
    sources = []
    for name, parser in (("PersistedSettings.json", _parse_persisted), ("input.ini", _parse_input_ini)):
        p = d / name
        if p.exists():
            try:
                e, q = parser(p)
                events.update(e)
                quick.update(q)
                sources.append(name)
            except Exception as ex:  # noqa: BLE001
                sources.append(f"{name} (unreadable: {ex})")
    if not sources:
        return Keybinds(events, quick, f"defaults: {d} has no input.ini yet")
    return Keybinds(events, quick, f"{d} ({', '.join(sources)})", from_config=True)


@dataclass
class GameCfg:
    width: int | None = None
    height: int | None = None
    window_mode: int | None = None
    minimap_scale: float | None = None
    global_scale: float | None = None
    source: str = "not found"


def load_game_cfg(config_dir: Path | None = None) -> GameCfg:
    d = config_dir or find_config_dir()
    if d is None or not (d / "game.cfg").exists():
        return GameCfg()
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment]
    cp.read(d / "game.cfg", encoding="utf-8-sig")
    g = GameCfg(source=str(d / "game.cfg"))

    def num(sec: str, key: str, cast):
        try:
            return cast(cp.get(sec, key))
        except Exception:  # noqa: BLE001
            return None

    g.width = num("General", "Width", int)
    g.height = num("General", "Height", int)
    g.window_mode = num("General", "WindowMode", int)
    g.minimap_scale = num("HUD", "MinimapScale", float)
    g.global_scale = num("HUD", "GlobalScale", float)
    return g
