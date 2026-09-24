"""macOS input through Quartz CGEvent. Standard accessibility events, nothing hidden from the OS.

Coordinates are logical screen points with the origin at the top-left of the main display,
which is what CGEvent expects. screen.py converts captured pixels to these points.
Requires Accessibility permission for the terminal app that launches the process.
"""
from __future__ import annotations

import collections
import contextlib
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


def session_on_console() -> bool:
    """True when this process's login session is the one on the physical screen."""
    try:
        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d.get("kCGSSessionOnConsoleKey", True)) if d is not None else True
    except Exception:  # noqa: BLE001
        return True


# Processes that draw macOS permission prompts and password sheets over whatever app is in front.
SYSTEM_PROMPT_OWNERS = ("UserNotificationCenter", "SecurityAgent", "CoreServicesUIAgent", "universalAccessAuthWarn",
                        "coreautha", "tccd")
_prompt_cache: tuple[float, str | None] = (0.0, None)


def system_prompt_on_screen() -> str | None:
    """The owner of a system permission prompt drawn over the screen, else None (cached 0.5 s).
    A "python3.14 wants access to control Codex Computer Use" prompt sat over the game with its
    Allow button where the harness clicks around the champion: no input may go out while one is up."""
    global _prompt_cache
    now = time.time()
    if now - _prompt_cache[0] < 0.5:
        return _prompt_cache[1]
    found = None
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
        for w in wins:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if owner in SYSTEM_PROMPT_OWNERS and int(w.get("kCGWindowLayer", 0)) >= 0:
                b = w.get("kCGWindowBounds", {}) or {}
                if float(b.get("Width", 0)) > 60 and float(b.get("Height", 0)) > 60:
                    found = owner
                    break
    except Exception:  # noqa: BLE001  no window list: assume none
        found = None
    _prompt_cache = (now, found)
    return found


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
        # On the console (your screen) input goes to the HID tap, the same path as a real keyboard.
        # In a background session (offstage's helper account) the HID tap would land on the console
        # user's screen, so input goes to this session's own tap instead.
        self.tap = kCGHIDEventTap if session_on_console() else Quartz.kCGSessionEventTap
        # Fast timings for in-game orders; slow() switches to UI-safe timings (shop, menus).
        from jev import config
        self.fast = config.FAST
        self._ui = 0
        self._orders: collections.deque[float] = collections.deque()

    @contextlib.contextmanager
    def slow(self):
        """UI timings: the shop and menus need a hover before the press and a longer hold."""
        self._ui += 1
        try:
            yield
        finally:
            self._ui -= 1

    def _hover_s(self) -> float:
        return 0.04 if self._ui else self.fast.hover_s

    def _hold_s(self, ui_ms: int) -> float:
        return ui_ms / 1000 if self._ui else self.fast.hold_s

    def _count(self) -> None:
        now = time.time()
        self._orders.append(now)
        while self._orders and now - self._orders[0] > 60:
            self._orders.popleft()

    def apm(self) -> int:
        """Orders (mouse presses and key presses) in the last minute, scaled up if fewer than 60 s have passed."""
        if not self._orders:
            return 0
        now = time.time()
        while self._orders and now - self._orders[0] > 60:
            self._orders.popleft()
        span = max(5.0, min(60.0, now - self._orders[0]))
        return int(len(self._orders) * 60 / span)

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
        if system_prompt_on_screen():
            self.blocked += 1
            return False
        return True

    def _post(self, ev) -> None:
        if self.to_pid and self.pid is not None:
            CGEventPostToPid(self.pid, ev)
        else:
            CGEventPost(self.tap, ev)

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
        time.sleep(self._hover_s())  # let the game register the cursor before the press
        if button == "left":
            down, up, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        else:
            down, up, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        self._post(CGEventCreateMouseEvent(None, down, (x, y), btn))
        self._count()
        time.sleep(self._hold_s(hold_ms))
        self._post(CGEventCreateMouseEvent(None, up, (x, y), btn))

    def drag(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> None:
        """Left-button drag from (x0, y0) to (x1, y1) (moving a UI panel by its title bar)."""
        self.log(f"drag({x0:.0f},{y0:.0f} -> {x1:.0f},{y1:.0f})")
        self.move(x0, y0)
        if not self._allowed():
            return
        time.sleep(0.06)
        self._post(CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x0, y0), kCGMouseButtonLeft))
        time.sleep(0.08)
        for k in range(1, steps + 1):
            x, y = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
            self._post(CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (x, y), kCGMouseButtonLeft))
            time.sleep(0.02)
        time.sleep(0.08)
        self._post(CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x1, y1), kCGMouseButtonLeft))
        self._pos = (x1, y1)

    def double_click(self, x: float, y: float) -> None:
        with self.slow():
            self.click(x, y, "left")
            time.sleep(0.08)
            self.click(x, y, "left")

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
        gap = 0.03 if self._ui else self.fast.mod_gap_s
        for _, mcode, mflag in active:
            self._post(CGEventCreateKeyboardEvent(None, mcode, True))
            flags |= mflag
            time.sleep(gap)
        down = CGEventCreateKeyboardEvent(None, code, True)
        up = CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            CGEventSetFlags(down, flags)
            CGEventSetFlags(up, flags)
        self._post(down)
        self._count()
        time.sleep(self._hold_s(hold_ms))
        self._post(up)
        for _, mcode, _ in reversed(active):
            time.sleep(gap)
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
                time.sleep(0.03)
            time.sleep(per_char_ms / 1000)

    # -- League verbs ------------------------------------------------------------
    def keys_ok(self) -> bool:
        """Input reaches the game only while it is the active app and no system prompt is up."""
        return not self.dry_run and game_is_frontmost() and not system_prompt_on_screen()

    def move_to(self, x: float, y: float) -> None:
        """Right click = move / attack the unit under the cursor."""
        self.click(x, y, "right")

    def attack_move_click(self, x: float, y: float) -> None:
        """Shift + right click = attack-move to the point (League's evtPlayerAttackMoveClick default)."""
        self.move(x, y)
        self.log(f"shift_click_right({x:.0f},{y:.0f})")
        if not self._allowed():
            return
        time.sleep(self._hover_s())
        down = CGEventCreateMouseEvent(None, kCGEventRightMouseDown, (x, y), kCGMouseButtonRight)
        up = CGEventCreateMouseEvent(None, kCGEventRightMouseUp, (x, y), kCGMouseButtonRight)
        CGEventSetFlags(down, kCGEventFlagMaskShift)
        CGEventSetFlags(up, kCGEventFlagMaskShift)
        self._post(down)
        self._count()
        time.sleep(self._hold_s(40))
        self._post(up)

    def attack_move(self, bind, x: float, y: float) -> None:
        """Attack-move key then left click: attacks the nearest unit on the way."""
        self.move(x, y)
        self.press(bind)
        time.sleep(0.02 if self._ui else self.fast.hover_s)
        self.click(x, y, "left")

    def _aim(self, x: float, y: float) -> None:
        """Cursor to (x, y) for a key that fires at the cursor. The game reads the cursor once per
        frame (~17 ms): 12 ms after one move event, Q went toward the previous move click, behind
        Yasuo, and 29 of 30 Q last hits missed (g29, the thrust pointing at the move marker). Two
        moves, the second one frame later, and the key after it."""
        self.move(x, y)
        if not self._allowed() or self._ui:
            time.sleep(self._hover_s())
            return
        time.sleep(self.fast.aim_s / 2)
        self._post(CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft))
        time.sleep(self.fast.aim_s / 2)

    def cast(self, bind, x: float, y: float, quick_cast: bool | None) -> None:
        """Quick cast fires at the cursor. Classic cast needs a confirming left click.

        When quick_cast is None (League config not read yet) the click is sent anyway: with
        quick cast on it is a harmless left click, with it off it confirms the cast.
        """
        self._aim(x, y)
        self.press(bind)
        if not quick_cast:
            time.sleep(self._hover_s())
            self.click(x, y, "left")

    def tap(self, bind, x: float, y: float) -> None:
        """Cursor to (x, y) and press a key: targeted spells with quick cast, or R on a target."""
        self._aim(x, y)
        self.press(bind)
