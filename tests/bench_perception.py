"""Perception timing: per-frame read cost on a saved lane frame, and live capture latency for
both backends (reads whatever is on screen; sends no input). Run: uv run python tests/bench_perception.py"""
import statistics
import threading
import time

import cv2

from jev import config
from jev.loop import Player
from jev.minimap import MinimapReader
from jev.screen import Screen
from jev.vision import VisionReader

img = cv2.cvtColor(cv2.imread("snapshots/054108.png"), cv2.COLOR_BGR2BGRA)
vr, mm = VisionReader(), MinimapReader(Screen())
x0, y0, s = config.GEOMETRY.minimap
for name, fn in (("vision", lambda f: vr.read(f)), ("minimap", lambda f: mm.read(f[y0:y0 + s, x0:x0 + s]))):
    ts = []
    for _ in range(40):
        a = time.perf_counter()
        fn(img)
        ts.append((time.perf_counter() - a) * 1000)
    print(f"{name:8} p50 {statistics.median(ts[5:]):.2f} ms")

for backend in ("sck", "mss"):
    p = Player(dry_run=False, capture=backend, decision_log=None)
    th = threading.Thread(target=p._perceive_loop, daemon=True)
    th.start()
    time.sleep(3.0)
    ages = []
    for _ in range(40):
        ages.append(p.frame_age_ms)
        time.sleep(0.02)
    p._stop.set()
    th.join(2)
    print(f"{p.capture_name:17} {p.perceive_fps:5.1f} fps  read {p.perceive_ms:.1f} ms  frame age after read p50 {statistics.median(ages):.1f} ms")
