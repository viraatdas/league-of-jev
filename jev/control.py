"""macOS input through Quartz CGEvent. Standard accessibility events, nothing hidden from the OS.

Coordinates are logical screen points with the origin at the top-left of the main display,
which is what CGEvent expects. screen.py converts captured pixels to these points.
Requires Accessibility permission for the terminal app that launches the process.
"""
from __future__ import annotations

import time

import Quartz
from Quartz import (
    CGEventKeyboardSetUnicodeString,
    CGEventPostToPid,
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventRightMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
)

# US layout virtual keycodes.
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "l": 37, "j": 38, "'": 39, "k": 40,
    ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49,
    "`": 50, "delete": 51, "escape": 53, "cmd": 55, "shift": 56, "alt": 58, "ctrl": 59,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "up": 126, "down": 125, "left": 123, "right": 124,
}


GAME_BUNDLE_HINT = "gameclient"  # com.riotgames.LeagueofLegends.GameClient


def frontmost_app_name() -> str:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return ""
        return f"{app.localizedName()} [{app.bundleIdentifier() or ''}]"
    except Exception:  # noqa: BLE001
        return ""


def _front_window_pid() -> int | None:
    """PID owning the frontmost normal window, from the window server (works in any process)."""
    try:
        from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly

        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
            if w.get("kCGWindowLayer") != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) < 200 or b.get("Height", 0) < 200:
                continue
            return int(w.get("kCGWindowOwnerPID"))
    except Exception:  # noqa: BLE001
        return None
    return None


def game_is_frontmost() -> bool:
    name = frontmost_app_name().lower()
    if name:
        return GAME_BUNDLE_HINT in name or "league of legends (tm) client" in name
    pid = game_pid()
    return pid is not None and _front_window_pid() == pid


def game_pid() -> int | None:
    try:
        from AppKit import NSWorkspace

        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if GAME_BUNDLE_HINT in str(app.bundleIdentifier() or "").lower():
                return int(app.processIdentifier())
    except Exception:  # noqa: BLE001
        return None
    return None


def activate_game() -> bool:
    """Bring the game process (not the lobby client) to the front. Tries AppKit activation,
    then System Events (needs the Accessibility permission the controller already requires)."""
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace

        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bid = str(app.bundleIdentifier() or "").lower()
            if GAME_BUNDLE_HINT in bid:
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                break
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.3)
    if game_is_frontmost():
        return True
    try:
        import subprocess

        script = ('tell application "System Events" to set frontmost of '
                  '(first process whose bundle identifier is "com.riotgames.LeagueofLegends.GameClient") to true')
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.3)
    return game_is_frontmost()


class Controller:
    """Posts mouse and keyboard events. With dry_run=True it only logs what it would do.

    Input is only sent while League is the frontmost app, so switching to the terminal
    (Cmd-Tab, then Ctrl-C) is always safe.
    """

    def __init__(self, dry_run: bool = False, log=None, require_frontmost: str | None = "League", to_pid: bool = False) -> None:
        self.dry_run = dry_run
        self.log = log or (lambda msg: None)
        self._pos = (0.0, 0.0)
        self.require_frontmost = require_frontmost
        self.to_pid = to_pid
        self.pid: int | None = game_pid() if to_pid else None
        self.blocked = 0

    def _allowed(self) -> bool:
        """Default: HID-tap events, only while the game is the active app. Verified through the
        game API: the game ignores input while another app is active, including events posted to
        its process, so to_pid stays off. Nothing is ever sent to a different app."""
        if self.dry_run:
            return False
        if self.to_pid:
            if self.pid is None:
                self.pid = game_pid()
            if self.pid is not None:
                return True
        if self.require_frontmost and not game_is_frontmost():
            self.blocked += 1
            return False
        return True

    def _post(self, ev) -> None:
        if self.to_pid and self.pid is not None:
            CGEventPostToPid(self.pid, ev)
        else:
            CGEventPost(kCGHIDEventTap, ev)

    # -- mouse -----------------------------------------------------------------
    def move(self, x: float, y: float) -> None:
        self._pos = (x, y)
        self.log(f"move({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
        self._post(ev)

    def click(self, x: float, y: float, button: str = "right", hold_ms: int = 40) -> None:
        self.move(x, y)
        self.log(f"click_{button}({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        time.sleep(0.04)  # let the UI register the hover before the press
        if button == "left":
            down, up, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        else:
            down, up, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        self._post(CGEventCreateMouseEvent(None, down, (x, y), btn))
        time.sleep(hold_ms / 1000)
        self._post(CGEventCreateMouseEvent(None, up, (x, y), btn))

    # -- keyboard --------------------------------------------------------------
    _MOD_CODES = (("ctrl", 59, kCGEventFlagMaskControl), ("shift", 56, kCGEventFlagMaskShift), ("alt", 58, kCGEventFlagMaskAlternate), ("cmd", 55, kCGEventFlagMaskCommand))

    def key(self, name: str, ctrl: bool = False, shift: bool = False, alt: bool = False, cmd: bool = False, hold_ms: int = 60) -> None:
        """Key press. Modifiers are sent as real key-down/up events around the key, with the
        matching flags on the key events: the game ignores a bare flag without the modifier press."""
        code = KEYCODES[name.lower()]
        wanted = {"ctrl": ctrl, "shift": shift, "alt": alt, "cmd": cmd}
        mods = "".join(f"{m}+" for m, on in wanted.items() if on)
        self.log(f"key({mods}{name})")
        if not self._allowed():
            return
        flags = 0
        active = [(m, c, f) for m, c, f in self._MOD_CODES if wanted[m]]
        for _, mcode, mflag in active:
            self._post(CGEventCreateKeyboardEvent(None, mcode, True))
            flags |= mflag
            time.sleep(0.03)
        down = CGEventCreateKeyboardEvent(None, code, True)
        up = CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            CGEventSetFlags(down, flags)
            CGEventSetFlags(up, flags)
        self._post(down)
        time.sleep(hold_ms / 1000)
        self._post(up)
        for _, mcode, _ in reversed(active):
            time.sleep(0.03)
            self._post(CGEventCreateKeyboardEvent(None, mcode, False))

    def hold(self, bind, down: bool) -> None:
        """Press or release a key without the matching up/down, for held keys like camera snap."""
        if bind.key is None:
            return
        self.log(f"hold({bind.key},{'down' if down else 'up'})")
        if not self._allowed():
            return
        self._post(CGEventCreateKeyboardEvent(None, KEYCODES[bind.key], down))

    def focus(self, x: float, y: float) -> None:
        """Activate the game process and left-click inside its window so it has keyboard focus."""
        activate_game()
        time.sleep(0.4)
        self.click(x, y, "left")
        time.sleep(0.2)

    def press(self, bind, hold_ms: int = 35) -> None:
        """Press a keybinds.Bind (key plus modifiers) exactly as League has it configured."""
        if bind.key is None:
            self.log(f"press(unbound {bind})")
            return
        self.key(bind.key, ctrl=bind.ctrl, shift=bind.shift, alt=bind.alt, cmd=bind.cmd, hold_ms=hold_ms)

    def type_text(self, text: str, per_char_ms: int = 25) -> None:
        """Text entry for UI fields: each key event carries the character as a Unicode string."""
        self.log(f"type({text!r})")
        if not self._allowed():
            return
        for ch in text:
            code = KEYCODES.get(ch.lower(), 0)
            for down in (True, False):
                ev = CGEventCreateKeyboardEvent(None, code, down)
                CGEventKeyboardSetUnicodeString(ev, 1, ch)
                self._post(ev)
                time.sleep(0.015)
            time.sleep(per_char_ms / 1000)

    # -- League verbs ------------------------------------------------------------
    def keys_ok(self) -> bool:
        """Input reaches the game only while it is the active app."""
        return not self.dry_run and game_is_frontmost()

    def move_to(self, x: float, y: float) -> None:
        """Right click = move / attack the unit under the cursor."""
        self.click(x, y, "right")

    def attack_move_click(self, x: float, y: float) -> None:
        """Shift + right click = attack-move to the point (League's evtPlayerAttackMoveClick default)."""
        self.move(x, y)
        self.log(f"shift_click_right({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        time.sleep(0.04)
        down = CGEventCreateMouseEvent(None, kCGEventRightMouseDown, (x, y), kCGMouseButtonRight)
        up = CGEventCreateMouseEvent(None, kCGEventRightMouseUp, (x, y), kCGMouseButtonRight)
        CGEventSetFlags(down, kCGEventFlagMaskShift)
        CGEventSetFlags(up, kCGEventFlagMaskShift)
        self._post(down)
        time.sleep(0.04)
        self._post(up)

    def attack_move(self, bind, x: float, y: float) -> None:
        """Attack-move key then left click: attacks the nearest unit on the way."""
        self.move(x, y)
        self.press(bind)
        time.sleep(0.02)
        self.click(x, y, "left")

    def cast(self, bind, x: float, y: float, quick_cast: bool | None) -> None:
        """Quick cast fires at the cursor. Classic cast needs a confirming left click.

        When quick_cast is None (League config not read yet) the click is sent anyway: with
        quick cast on it is a harmless left click, with it off it confirms the cast.
        """
        self.move(x, y)
        time.sleep(0.01)
        self.press(bind)
        if not quick_cast:
            time.sleep(0.03)
            self.click(x, y, "left")
