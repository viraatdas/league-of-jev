"""macOS input through Quartz CGEvent. Standard accessibility events, nothing hidden from the OS.

Coordinates are logical screen points with the origin at the top-left of the main display,
which is what CGEvent expects. screen.py converts captured pixels to these points.
Requires Accessibility permission for the terminal app that launches the process.
"""
from __future__ import annotations

import time

import Quartz
from Quartz import (
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


def frontmost_app_name() -> str:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app is not None else ""
    except Exception:  # noqa: BLE001
        return ""


class Controller:
    """Posts mouse and keyboard events. With dry_run=True it only logs what it would do.

    Input is only sent while League is the frontmost app, so switching to the terminal
    (Cmd-Tab, then Ctrl-C) is always safe.
    """

    def __init__(self, dry_run: bool = False, log=None, require_frontmost: str | None = "League") -> None:
        self.dry_run = dry_run
        self.log = log or (lambda msg: None)
        self._pos = (0.0, 0.0)
        self.require_frontmost = require_frontmost
        self.blocked = 0

    def _allowed(self) -> bool:
        if self.dry_run:
            return False
        if self.require_frontmost and self.require_frontmost.lower() not in frontmost_app_name().lower():
            self.blocked += 1
            return False
        return True

    # -- mouse -----------------------------------------------------------------
    def move(self, x: float, y: float) -> None:
        self._pos = (x, y)
        self.log(f"move({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, ev)

    def click(self, x: float, y: float, button: str = "right", hold_ms: int = 25) -> None:
        self.move(x, y)
        self.log(f"click_{button}({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        if button == "left":
            down, up, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        else:
            down, up, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, down, (x, y), btn))
        time.sleep(hold_ms / 1000)
        CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, up, (x, y), btn))

    # -- keyboard --------------------------------------------------------------
    def key(self, name: str, ctrl: bool = False, shift: bool = False, alt: bool = False, cmd: bool = False, hold_ms: int = 35) -> None:
        code = KEYCODES[name.lower()]
        mods = "".join(m for m, on in (("ctrl+", ctrl), ("shift+", shift), ("alt+", alt), ("cmd+", cmd)) if on)
        self.log(f"key({mods}{name})")
        if not self._allowed():
            return
        flags = 0
        if ctrl:
            flags |= kCGEventFlagMaskControl
        if shift:
            flags |= kCGEventFlagMaskShift
        if alt:
            flags |= kCGEventFlagMaskAlternate
        if cmd:
            flags |= kCGEventFlagMaskCommand
        down = CGEventCreateKeyboardEvent(None, code, True)
        up = CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            CGEventSetFlags(down, flags)
            CGEventSetFlags(up, flags)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(hold_ms / 1000)
        CGEventPost(kCGHIDEventTap, up)

    def press(self, bind, hold_ms: int = 35) -> None:
        """Press a keybinds.Bind (key plus modifiers) exactly as League has it configured."""
        if bind.key is None:
            self.log(f"press(unbound {bind})")
            return
        self.key(bind.key, ctrl=bind.ctrl, shift=bind.shift, alt=bind.alt, cmd=bind.cmd, hold_ms=hold_ms)

    def type_text(self, text: str, per_char_ms: int = 20) -> None:
        for ch in text:
            if ch == " ":
                self.key("space")
            elif ch.lower() in KEYCODES:
                self.key(ch.lower(), shift=ch.isupper())
            time.sleep(per_char_ms / 1000)

    # -- League verbs ------------------------------------------------------------
    def move_to(self, x: float, y: float) -> None:
        """Right click = move / attack the unit under the cursor."""
        self.click(x, y, "right")

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
