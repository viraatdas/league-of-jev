"""Screen capture. ScreenCaptureKit streams the display as it refreshes (frames arrive about one
refresh after they are drawn, nothing blocks); mss is the fallback, a blocking ~29 ms grab.

One stream of the whole display: the game view, the minimap and the HUD come from the same
frame, so positions, minions and cooldowns always agree. The stream is scaled to the
calibrated frame size (config geometry is in those pixels) and leaves out this process's own
windows, so the overlay never shows up in what the bot reads.

Frames are BGRA uint8 arrays; `ts` is when the frame was captured (time.time() clock).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    img: np.ndarray   # H x W x 4, BGRA
    ts: float         # capture time
    seq: int


class _Latest:
    def __init__(self) -> None:
        self.frame: Frame | None = None
        self.cv = threading.Condition()
        self.seq = 0

    def put(self, img: np.ndarray, ts: float) -> None:
        with self.cv:
            self.seq += 1
            self.frame = Frame(img, ts, self.seq)
            self.cv.notify_all()

    def wait(self, after_seq: int, timeout: float) -> Frame | None:
        with self.cv:
            if self.frame is None or self.frame.seq <= after_seq:
                self.cv.wait(timeout)
            f = self.frame
            return f if f is not None and f.seq > after_seq else None


class MssCapture:
    name = "mss"

    def __init__(self, width: int | None = None, height: int | None = None) -> None:
        import mss

        self.sct = mss.mss()
        self.mon = self.sct.monitors[1]
        self.seq = 0

    def start(self) -> bool:
        return True

    def wait(self, after_seq: int, timeout: float = 0.2) -> Frame | None:
        t = time.time()
        img = np.array(self.sct.grab(self.mon))
        self.seq += 1
        return Frame(img, t, self.seq)

    def stop(self) -> None:
        pass


class SCKCapture:
    name = "screencapturekit"

    def __init__(self, width: int, height: int, fps: int = 60) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.latest = _Latest()
        self.stream = None
        self.error: str = ""
        self.frames = 0

    def _on_sample(self, sbuf) -> None:
        import CoreMedia
        import Quartz

        pb = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
        if pb is None:  # idle frame: nothing changed on screen
            return
        try:
            # Capture time: the frame's presentation time on the host clock, mapped to time.time().
            pts = CoreMedia.CMTimeGetSeconds(CoreMedia.CMSampleBufferGetPresentationTimeStamp(sbuf))
            host_now = CoreMedia.CMTimeGetSeconds(CoreMedia.CMClockGetTime(CoreMedia.CMClockGetHostTimeClock()))
            ts = time.time() - max(0.0, host_now - pts)
        except Exception:  # noqa: BLE001
            ts = time.time()
        Quartz.CVPixelBufferLockBaseAddress(pb, 1)  # read-only
        try:
            h, w = Quartz.CVPixelBufferGetHeight(pb), Quartz.CVPixelBufferGetWidth(pb)
            bpr = Quartz.CVPixelBufferGetBytesPerRow(pb)
            buf = Quartz.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)
            img = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr // 4, 4)[:, :w].copy()
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)
        self.frames += 1
        self.latest.put(img, ts)

    def start(self, timeout: float = 5.0) -> bool:
        try:
            import CoreMedia
            import objc
            import Quartz
            import ScreenCaptureKit as SCK
            from Foundation import NSObject
        except Exception as e:  # noqa: BLE001
            self.error = f"ScreenCaptureKit unavailable: {e}"
            return False

        done = threading.Event()
        box: dict = {}

        def got_content(content, error):
            box["content"], box["error"] = content, error
            done.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
        if not done.wait(timeout) or box.get("content") is None:
            self.error = f"no shareable content: {box.get('error')}"
            return False
        content = box["content"]
        main_id = Quartz.CGMainDisplayID()
        display = next((d for d in content.displays() if d.displayID() == main_id), content.displays()[0])
        me = [a for a in content.applications() if a.processID() == os.getpid()]
        flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(display, me, [])

        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(self.width)
        cfg.setHeight_(self.height)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, self.fps))
        cfg.setPixelFormat_(1111970369)  # kCVPixelFormatType_32BGRA ('BGRA')
        cfg.setQueueDepth_(3)
        cfg.setShowsCursor_(False)

        owner = self

        class _Out(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
                if kind == SCK.SCStreamOutputTypeScreen:
                    try:
                        owner._on_sample(sbuf)
                    except Exception as e:  # noqa: BLE001
                        owner.error = f"frame error: {e}"

        self._out = _Out.alloc().init()
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, None)
        ok, err = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(self._out, SCK.SCStreamOutputTypeScreen, None, None)
        if not ok:
            self.error = f"addStreamOutput failed: {err}"
            return False
        started = threading.Event()

        def on_start(error):
            box["start_error"] = error
            started.set()

        self.stream.startCaptureWithCompletionHandler_(on_start)
        if not started.wait(timeout) or box.get("start_error") is not None:
            self.error = f"start failed: {box.get('start_error')}"
            return False
        return True

    def wait(self, after_seq: int, timeout: float = 0.2) -> Frame | None:
        return self.latest.wait(after_seq, timeout)

    def stop(self) -> None:
        if self.stream is not None:
            done = threading.Event()
            self.stream.stopCaptureWithCompletionHandler_(lambda e: done.set())
            done.wait(2.0)
            self.stream = None


def open_capture(width: int, height: int, prefer: str = "sck", fps: int = 60) -> MssCapture | SCKCapture:
    """ScreenCaptureKit when available and permitted, else mss."""
    if prefer == "sck":
        cap = SCKCapture(width, height, fps=fps)
        if cap.start():
            return cap
    m = MssCapture()
    m.start()
    return m
