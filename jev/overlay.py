"""On-screen overlay: a movable panel with every Jev answer, and a marker layer over the game.

Panel: what Jev is choosing from and what it chose. The tactical menu with a probability bar
per move (chosen move highlighted, exploration marked), the target / where / distance answers,
the strategy head (intent bars, danger, recall, fight, aggression, destination, level-up), the
build head (item, needs, what to buy), and speed (APM, capture, screen-to-input latency).

Moving it: the panel is click-through while the game is the active app, so the bot's clicks
never land on it. Hold Cmd to grab and drag it during play; when the game is not active (the
bot is paused) it can be dragged freely. Double-click toggles a compact view. The position and
mode are saved to ~/.config/league-of-jev/overlay.json.

Markers: a transparent, click-through layer over the whole screen that draws the last executed
move (a ring on the target, a line to the aimed point), killable minions, and the enemy
champion's HP. Both windows belong to this process, which the bot's screen capture excludes,
so nothing drawn here is ever read back as a game unit.

Both windows run on the main thread (AppKit); the play loop runs in a background thread and
the overlay pulls a snapshot dict from it ten times a second.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSEventModifierFlagCommand,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakePoint,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSString

STATE_FILE = Path.home() / ".config" / "league-of-jev" / "overlay.json"
W = 410


def _c(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


BG, EDGE = _c(0.05, 0.07, 0.10, 0.94), _c(1, 1, 1, 0.10)
FG, DIM, FAINT = _c(0.91, 0.94, 0.97), _c(0.58, 0.63, 0.70), _c(1, 1, 1, 0.07)
ACCENT, WARN, BAD, GOOD, GOLD = _c(0.30, 0.86, 0.76), _c(1.0, 0.64, 0.26), _c(0.96, 0.36, 0.36), _c(0.45, 0.85, 0.40), _c(1.0, 0.84, 0.35)


def _font(size: float, bold: bool = False):
    return NSFont.monospacedSystemFontOfSize_weight_(size, 0.35 if bold else 0.0)


def _text(s: str, x: float, y: float, size: float = 11, color=FG, bold: bool = False) -> float:
    attrs = {NSFontAttributeName: _font(size, bold), NSForegroundColorAttributeName: color}
    ns = NSString.stringWithString_(str(s))
    ns.drawAtPoint_withAttributes_(NSMakePoint(x, y), attrs)
    return ns.sizeWithAttributes_(attrs).width


def _rect(x, y, w, h, color, r: float = 2.0) -> None:
    color.setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, max(0.0, w), h), r, r).fill()


def _bar(x, y, w, frac, color, h: float = 7) -> None:
    _rect(x, y, w, h, FAINT)
    _rect(x, y, w * max(0.0, min(1.0, frac)), h, color)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d))
    except Exception:  # noqa: BLE001
        pass


# --- panel ---------------------------------------------------------------------------------
class PanelView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(PanelView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.data = {}
        self.compact = False
        self.draggable = False
        self.on_toggle = None
        self.needed_h = 200.0
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDownCanMoveWindow(self):
        return True

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            self.compact = not self.compact
            if self.on_toggle:
                self.on_toggle()
            self.setNeedsDisplay_(True)
            return
        self.window().performWindowDragWithEvent_(event)

    def drawRect_(self, rect):
        b = self.bounds()
        _rect(0, 0, b.size.width, b.size.height, BG, 9)
        EDGE.setStroke()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0.5, 0.5, b.size.width - 1, b.size.height - 1), 9, 9)
        path.setLineWidth_(1.0)
        path.stroke()
        if self.draggable:
            ACCENT.setStroke()
            path.setLineWidth_(2.0)
            path.stroke()
        try:
            self.needed_h = self._paint(self.data or {})
        except Exception as e:  # noqa: BLE001  never let a bad snapshot kill the overlay
            _text(f"overlay error: {e}"[:60], 12, 12, 10, BAD)

    # -- sections ----------------------------------------------------------------------
    @objc.python_method
    def _section(self, y: float, title: str, right: str = "") -> float:
        _text(title, 12, y, 9.5, DIM, bold=True)
        if right:
            _text(right, W - 12 - 6.2 * len(right), y, 9.5, DIM)
        _rect(12, y + 15, W - 24, 1, FAINT, 0)
        return y + 21

    @objc.python_method
    def _paint(self, d: dict) -> float:
        h, y = d.get("header", {}), 10.0
        _text("JEV", 12, y, 13, ACCENT, bold=True)
        _text(h.get("title", "waiting for a game"), 48, y + 1, 11.5, FG, bold=True)
        live = h.get("input", "")
        _text(live, W - 12 - 6.6 * len(live), y + 1, 10.5, GOOD if live == "live" else WARN)
        y += 19
        if h.get("stats"):
            _text(h["stats"], 12, y, 10.5, DIM)
            y += 17
        t = d.get("tactics") or {}
        if self.compact:
            if t:
                _text(f"> {t.get('action', '').upper()}  {t.get('p', 0):.2f}", 12, y, 12, WARN if t.get("explored") else ACCENT, bold=True)
                y += 18
            s = d.get("strategy") or {}
            if s:
                _text(f"plan {s.get('intent', '').upper()}   danger {s.get('danger', 0):.1f}   APM {d.get('perf', {}).get('apm', 0)}", 12, y, 10.5, DIM)
                y += 16
            _text("double-click to expand", 12, y, 9, DIM)
            return y + 18
        y = self._tactics(y, t)
        y = self._strategy(y, d.get("strategy") or {})
        y = self._build(y, d.get("build") or {})
        y = self._perf(y, d.get("perf") or {}, d.get("log") or [])
        _text("drag: hold ⌘ (or when the game is not active) · double-click: compact", 12, y + 2, 8.5, DIM)
        return y + 18

    @objc.python_method
    def _tactics(self, y: float, t: dict) -> float:
        y = self._section(y + 4, "TACTICS  ·  the move menu", f"{t.get('latency', 0):.0f} ms  {t.get('rate', 0):.1f}/s" if t else "")
        if not t:
            _text("no units on screen: macro movement", 12, y, 10.5, DIM)
            return y + 18
        col = WARN if t.get("explored") else ACCENT
        head = f"> {t.get('action', '').upper()}"
        wdt = _text(head, 12, y, 13.5, col, bold=True)
        _text(f"p {t.get('p', 0):.2f}" + ("   (explore)" if t.get("explored") else ""), 20 + wdt, y + 2, 10.5, col)
        fits = t.get("fits", 1.0)
        _text(f"menu fits {fits:.2f}", W - 118, y + 2, 10, GOOD if fits >= 0.5 else WARN)
        y += 22
        menu = t.get("menu") or []
        shown = [(n, p) for n, p in menu if p >= 0.02 or n == t.get("action")][:10]
        hidden = [n for n, p in menu if (n, p) not in shown]
        for name, p in shown:
            chosen = name == t.get("action")
            if chosen:
                _rect(8, y - 2, W - 16, 15, _c(0.30, 0.86, 0.76, 0.12), 3)
            _text(name, 16, y, 10.5, FG if chosen else DIM, bold=chosen)
            _bar(138, y + 3, W - 200, p, col if chosen else _c(0.55, 0.62, 0.72, 0.9))
            _text(f"{p:.2f}", W - 52, y, 10.5, FG if chosen else DIM)
            y += 15
        if hidden:
            line = f"+{len(hidden)} more under 0.02: " + ", ".join(hidden)
            _text(line[:64] + ("..." if len(line) > 64 else ""), 16, y, 9, DIM)
            y += 13
        y += 4
        for label, key in (("target", "target"), ("where", "where")):
            opts = t.get(key) or []
            if not opts:
                continue
            best = opts[0]
            _text(f"{label:<7}", 12, y, 10.5, DIM)
            _text(f"{best[2] if len(best) > 2 and best[2] else best[0]}", 70, y, 10.5, FG)
            _text(f"{best[1]:.2f}", W - 52, y, 10.5, DIM)
            y += 14
            rest = "  ".join(f"{o[0]} {o[1]:.2f}" for o in opts[1:3])
            if rest:
                _text(rest, 70, y, 9, DIM)
                y += 13
        if t.get("distance") is not None:
            dist = t["distance"]
            _text("distance", 12, y, 10.5, DIM)
            _bar(80, y + 3, 150, dist / 2.0, ACCENT)
            _text(["short", "medium", "long"][max(0, min(2, int(round(dist))))], 240, y, 10.5, FG)
            y += 15
        if t.get("executed"):
            _text(f"did: {t['executed']}", 12, y, 10.5, GOLD)
            _text(f"{t.get('exec_age', 0):.1f} s ago", W - 80, y, 10, DIM)
            y += 15
        return y + 2

    @objc.python_method
    def _strategy(self, y: float, s: dict) -> float:
        if not s:
            return y
        y = self._section(y + 4, "STRATEGY  ·  once a second", f"{s.get('latency', 0):.0f} ms")
        _text(f"plan {s.get('intent', '').upper()}", 12, y, 12, ACCENT, bold=True)
        _text(f"jev: {s.get('jev_intent', '')} {s.get('p', 0):.2f}", 190, y + 1, 10.5, DIM)
        y += 18
        for name, p in (s.get("probs") or [])[:4]:
            _text(name, 16, y, 10, DIM)
            _bar(110, y + 3, 140, p, _c(0.55, 0.62, 0.72, 0.9), 6)
            _text(f"{p:.2f}", 258, y, 10, DIM)
            y += 13
        y += 3
        gauges = (("danger", s.get("danger", 0) / 3.0, BAD, f"{s.get('danger', 0):.1f}/3"),
                  ("aggr", s.get("aggr", 0) / 2.0, WARN, f"{s.get('aggr', 0):.1f}/2"),
                  ("recall", s.get("recall", 0), ACCENT, f"{s.get('recall', 0):.2f}"),
                  ("fight", s.get("fight", 0), GOOD, f"{s.get('fight', 0):.2f}"))
        for i, (name, frac, color, val) in enumerate(gauges):
            x = 12 + (i % 2) * 198
            if i % 2 == 0 and i:
                y += 15
            _text(name, x, y, 10, DIM)
            _bar(x + 52, y + 3, 90, frac, color, 6)
            _text(val, x + 148, y, 10, DIM)
        y += 17
        dest = s.get("destination") or []
        if dest:
            _text("map", 12, y, 10, DIM)
            _text("  ".join(f"{n} {p:.2f}" for n, p in dest[:3]), 52, y, 10, FG)
            y += 14
        if s.get("level_up"):
            _text(f"level-up pick: {s['level_up']}", 12, y, 10, GOLD)
            y += 14
        return y + 4

    @objc.python_method
    def _build(self, y: float, b: dict) -> float:
        if not b:
            return y
        y = self._section(y + 4, "BUILD  ·  items vs this enemy team")
        _text(f"-> {b.get('target', '')}", 12, y, 11, GOLD, bold=True)
        _text(f"p {b.get('p', 0):.2f}", W - 70, y, 10.5, DIM)
        y += 16
        if b.get("buy_now"):
            _text("buy now: " + ", ".join(b["buy_now"]), 12, y, 10, FG)
            y += 14
        needs = b.get("needs") or {}
        for i, (name, p) in enumerate(needs.items()):
            x = 12 + (i % 3) * 132
            if i % 3 == 0 and i:
                y += 14
            _text(name[:8], x, y, 9.5, DIM)
            _bar(x + 58, y + 3, 60, p, WARN if p >= 0.6 else _c(0.55, 0.62, 0.72, 0.9), 6)
        return y + 18

    @objc.python_method
    def _perf(self, y: float, p: dict, log: list[str]) -> float:
        y = self._section(y + 4, "SPEED")
        _text(f"APM {p.get('apm', 0):<4}  {p.get('capture', '-')} {p.get('fps', 0):.0f} fps  read {p.get('read_ms', 0):.1f} ms  api {p.get('api_ms', 0):.0f} ms", 12, y, 10, FG)
        y += 14
        react = p.get("react") or {}
        if react:
            _text("screen->input " + "  ".join(f"{k} {v[0]:.0f}/{v[1]:.0f}" for k, v in react.items()) + " ms", 12, y, 10, DIM)
            y += 14
        for line in log[-3:]:
            _text(str(line)[:58], 12, y, 9.5, DIM)
            y += 13
        return y


# --- markers --------------------------------------------------------------------------------
class MarkerView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(MarkerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.data = {}
        return self

    def isFlipped(self):
        return True

    @objc.python_method
    def _label(self, s: str, x: float, y: float, color) -> None:
        w = 7.4 * len(s) + 12
        _rect(x - 5, y - 2, w, 19, _c(0.03, 0.04, 0.06, 0.78), 5)
        _text(s, x + 1, y, 12, color, bold=True)

    @objc.python_method
    def _circle(self, x, y, r, color, width=2.0, fill=None):
        p = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r, y - r, 2 * r, 2 * r))
        if fill is not None:
            fill.setFill()
            p.fill()
        color.setStroke()
        p.setLineWidth_(width)
        p.stroke()

    def drawRect_(self, rect):
        m = self.data or {}
        try:
            me = m.get("me")
            if me:
                self._circle(me[0], me[1], 30, _c(0.30, 0.86, 0.76, 0.45), 1.5)
            for x, y in m.get("killable") or []:
                self._circle(x, y, 7, GOLD, 2.0)
            ch = m.get("champ")
            if ch:
                self._circle(ch[0], ch[1], 26, _c(0.96, 0.36, 0.36, 0.8), 2.0)
                self._label(f"{ch[3]} {ch[2]:.0f}%", ch[0] + 30, ch[1] - 22, BAD)
            ex = m.get("exec")
            if ex and ex.get("age", 9) < 1.4:
                a = max(0.0, 1.0 - ex["age"] / 1.4)
                col = _c(1.0, 0.64, 0.26, a)
                lx, ly = (me or (0, 0))
                if ex.get("point") and me:
                    px, py = ex["point"]
                    line = NSBezierPath.bezierPath()
                    line.moveToPoint_(NSMakePoint(lx, ly))
                    line.lineToPoint_(NSMakePoint(px, py))
                    line.setLineWidth_(2.5)
                    line.setLineDash_count_phase_([8.0, 5.0], 2, 0.0)
                    col.setStroke()
                    line.stroke()
                    self._circle(px, py, 9, col, 2.5)
                    lx, ly = px, py
                if ex.get("target"):
                    tx, ty = ex["target"]
                    self._circle(tx, ty, 22, col, 3.0)
                    lx, ly = tx, ty
                self._label(ex.get("name", ""), lx + 30, ly + 2, col)
        except Exception:  # noqa: BLE001
            pass


# --- controller -----------------------------------------------------------------------------
class _Timer(NSObject):
    def initWithFn_(self, fn):
        self = objc.super(_Timer, self).init()
        self.fn = fn
        return self

    def tick_(self, _timer):
        try:
            self.fn()
        except Exception:  # noqa: BLE001
            pass

    def windowDidMove_(self, note):
        try:
            self.fn(moved=True)
        except TypeError:
            pass


class Overlay:
    def __init__(self, snapshot: Callable[[], dict], corner: str = "tr", markers: bool = True,
                 game_active: Callable[[], bool] | None = None) -> None:
        self.snapshot = snapshot
        self.game_active = game_active or (lambda: False)
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        screen = NSScreen.mainScreen().frame()
        self.screen_h = screen.size.height
        st = _load_state()
        h = 420.0
        if "x" in st and "top" in st:
            x, top = float(st["x"]), float(st["top"])
        else:
            x = 12 if corner in ("tl", "bl") else screen.size.width - W - 12
            top = 135 if corner in ("tl", "tr") else screen.size.height - 200 - h
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, self.screen_h - top - h, W, h), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(1001)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                          | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.view = PanelView.alloc().initWithFrame_(NSMakeRect(0, 0, W, h))
        self.view.compact = bool(st.get("compact", False))
        self.view.on_toggle = self._save
        self.panel.setContentView_(self.view)
        self.panel.orderFrontRegardless()

        self.marker_win = None
        if markers:
            self.marker_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                screen, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            self.marker_win.setLevel_(1000)
            self.marker_win.setOpaque_(False)
            self.marker_win.setBackgroundColor_(NSColor.clearColor())
            self.marker_win.setIgnoresMouseEvents_(True)
            self.marker_win.setHasShadow_(False)
            self.marker_win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                                   | NSWindowCollectionBehaviorFullScreenAuxiliary)
            self.markers = MarkerView.alloc().initWithFrame_(NSMakeRect(0, 0, screen.size.width, screen.size.height))
            self.marker_win.setContentView_(self.markers)
            self.marker_win.orderFrontRegardless()
        self.panel.orderFrontRegardless()

        self._target = _Timer.alloc().initWithFn_(self._refresh)
        self._mover = _Timer.alloc().initWithFn_(lambda moved=False: self._save())
        self.panel.setDelegate_(self._mover)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.1, self._target, "tick:", None, True)

    def _save(self) -> None:
        f = self.panel.frame()
        _save_state({"x": f.origin.x, "top": self.screen_h - (f.origin.y + f.size.height), "compact": bool(self.view.compact)})

    def _refresh(self) -> None:
        data = self.snapshot() or {}
        self.view.data = data
        # Click-through while the bot plays; grabbable with Cmd held or when the game is not active.
        cmd = bool(NSEvent.modifierFlags() & NSEventModifierFlagCommand)
        grab = cmd or not self.game_active()
        if grab != self.view.draggable:
            self.view.draggable = grab
            self.panel.setIgnoresMouseEvents_(not grab)
        # Resize to the content, keeping the top edge where the user put it.
        need = float(self.view.needed_h)
        f = self.panel.frame()
        if abs(f.size.height - need) > 2:
            top = f.origin.y + f.size.height
            self.panel.setFrame_display_(NSMakeRect(f.origin.x, top - need, W, need), True)
            self.view.setFrame_(NSMakeRect(0, 0, W, need))
        self.view.setNeedsDisplay_(True)
        if self.marker_win is not None:
            self.markers.data = data.get("markers") or {}
            self.markers.setNeedsDisplay_(True)
        self.panel.orderFrontRegardless()

    def run_forever(self) -> None:
        # Ctrl-C in the terminal quits: AppKit's run loop would otherwise swallow the interrupt.
        # The 10 Hz timer keeps Python running, so the handler fires within 0.1 s.
        import os
        import signal

        signal.signal(signal.SIGINT, lambda *_: os._exit(0))
        NSApp.run()


def run_with_overlay(worker: Callable[[], None], snapshot: Callable[[], dict], corner: str = "tr", markers: bool = True,
                     game_active: Callable[[], bool] | None = None) -> None:
    """Start `worker` in a background thread and run the overlay on the main thread."""
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    Overlay(snapshot, corner=corner, markers=markers, game_active=game_active).run_forever()
