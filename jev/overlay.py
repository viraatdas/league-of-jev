"""Always-on-top, click-through overlay panel showing Jev's latest decision and probabilities.
Runs on the main thread (AppKit); the play loop runs in a background thread and publishes a
snapshot dict that a timer renders. The panel never activates, so the game stays the active app."""
from __future__ import annotations

import threading
from typing import Callable

from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSTextField,
    NSTimer,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
import objc
from Foundation import NSObject


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


class Overlay:
    def __init__(self, snapshot: Callable[[], str], x: int = 12, y_from_top: int = 70, w: int = 470, h: int = 250) -> None:
        self.snapshot = snapshot
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        screen = NSScreen.mainScreen().frame()
        rect = NSMakeRect(x, screen.size.height - y_from_top - h, w, h)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(rect, style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(1000)  # above normal windows
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.05, 0.07, 0.10, 0.78))
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)
        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 6, w - 20, h - 12))
        self.label.setEditable_(False)
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setSelectable_(False)
        self.label.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.92, 0.95, 0.98, 1.0))
        self.label.setFont_(NSFont.userFixedPitchFontOfSize_(12.0))
        self.label.setMaximumNumberOfLines_(0)
        self.label.setLineBreakMode_(0)
        self.panel.contentView().addSubview_(self.label)
        self.panel.orderFrontRegardless()
        self._timer_target = _Timer.alloc().initWithFn_(self._refresh)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.25, self._timer_target, "tick:", None, True)

    def _refresh(self) -> None:
        self.label.setStringValue_(self.snapshot())
        self.panel.orderFrontRegardless()

    def run_forever(self) -> None:
        NSApp.run()


def run_with_overlay(worker: Callable[[], None], snapshot: Callable[[], str]) -> None:
    """Start `worker` in a background thread and run the overlay on the main thread."""
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    Overlay(snapshot).run_forever()


def format_snapshot(state: dict, decision, intent: str, mm_summary: str, last_action: str, keys_ok: bool) -> str:
    me = state.get("me", {}) if state else {}
    lines = []
    lines.append(f"JEV  t={state.get('game', {}).get('time') if state else '-'}  {me.get('champion', '')} L{me.get('level', '')}  HP {me.get('hp_percent', '')}%  gold {me.get('gold', '')}  cs {me.get('cs', '')}  {me.get('kda', '')}")
    if decision is None:
        lines.append("waiting for first decision...")
    else:
        probs = sorted(decision.intent_probabilities.items(), key=lambda kv: -kv[1])
        lines.append(f"intent -> {intent.upper()}   (jev: {decision.intent} p={decision.intent_confidence:.2f}, {decision.latency_ms:.0f} ms)")
        for name, pr in probs[:6]:
            bar = "#" * int(round(pr * 24))
            lines.append(f"  {name:<10} {pr:4.2f} {bar}")
        lines.append(f"danger {decision.danger:.1f}/3   recall {decision.should_recall:.2f}   fight {decision.fight_favorable:.2f}   aggr {decision.aggression:.1f}/2")
        lines.append(f"next item: {decision.next_item} (p={decision.next_item_confidence:.2f})")
    nearby = state.get("nearby", {}) if state else {}
    lines.append(f"{nearby.get('where_i_am', '')} | {nearby.get('minion_wave', '')} | {nearby.get('closest_enemy_champion', '')}")
    lines.append(f"minimap {mm_summary}")
    lines.append(f"act: {last_action}   input: {'live' if keys_ok else 'paused (game not active)'}")
    return "\n".join(lines)
