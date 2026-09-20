"""Screen capture with mss and the pixel-to-point mapping needed on Retina displays."""
from __future__ import annotations

from pathlib import Path

import cv2
import mss
import numpy as np
import Quartz


class Screen:
    def __init__(self, monitor_index: int = 1) -> None:
        self.sct = mss.mss()
        self.mon = self.sct.monitors[monitor_index]
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        self.logical_w = float(bounds.size.width)
        self.logical_h = float(bounds.size.height)
        self.px_w = int(self.mon["width"])
        self.px_h = int(self.mon["height"])
        self.scale = self.px_w / self.logical_w if self.logical_w else 1.0

    def grab(self) -> np.ndarray:
        """BGR frame in physical pixels."""
        shot = self.sct.grab(self.mon)
        return np.array(shot)[:, :, :3]

    def to_points(self, px: float, py: float) -> tuple[float, float]:
        return px / self.scale, py / self.scale

    def to_pixels(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale, y * self.scale

    def center_px(self) -> tuple[float, float]:
        return self.px_w / 2, self.px_h / 2

    def save(self, path: str | Path, frame: np.ndarray | None = None) -> Path:
        frame = self.grab() if frame is None else frame
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), frame)
        return path

    def describe(self) -> str:
        return f"{self.px_w}x{self.px_h} px, {self.logical_w:.0f}x{self.logical_h:.0f} pt, scale {self.scale:.1f}"
