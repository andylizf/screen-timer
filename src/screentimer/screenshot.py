"""Periodic screenshot capture using CoreGraphics."""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from AppKit import NSScreen  # type: ignore
from Quartz import (  # type: ignore
    CGDisplayCreateImage,
    CGImageDestinationAddImage,
    CGImageDestinationCreateWithData,
    CGImageDestinationFinalize,
)
from CoreFoundation import (  # type: ignore
    CFDataCreateMutable,
    CFRelease,
)

from screentimer.processor import CapturedImage


class ScreenshotCaptureManager:
    """Captures periodic screenshots for all active displays."""

    def __init__(
        self,
        frame_handler: Callable[[CapturedImage], None],
        *,
        capture_interval: float,
    ) -> None:
        if capture_interval <= 0:
            raise ValueError("capture_interval must be positive")

        self._frame_handler = frame_handler
        self._interval = capture_interval
        self._stop_event = threading.Event()
        self._threads: Dict[int, threading.Thread] = {}
        self._last_emit: Dict[int, float] = {}
        self._lock = threading.Lock()

    # Public API -------------------------------------------------------

    def start(self) -> None:
        display_ids = self._enumerate_displays()
        if not display_ids:
            raise RuntimeError("No displays available for screenshot capture")

        for display_id in display_ids:
            thread = threading.Thread(
                target=self._capture_loop,
                args=(display_id,),
                name=f"screenshot-{display_id}",
                daemon=True,
            )
            self._threads[display_id] = thread
            thread.start()
            logging.info(
                "ScreenshotCaptureManager started for display %s (interval %.1fs)",
                display_id,
                self._interval,
            )

    def stop(self) -> None:
        self._stop_event.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=self._interval + 1.0)
        self._threads.clear()
        self._last_emit.clear()
        logging.info("ScreenshotCaptureManager stopped")

    # Internal helpers -------------------------------------------------

    def _enumerate_displays(self) -> List[int]:
        screens = NSScreen.screens()
        display_ids: List[int] = []
        for screen in screens or []:
            desc = screen.deviceDescription()
            display_number = desc.get("NSScreenNumber") if desc is not None else None
            if display_number is not None:
                display_ids.append(int(display_number))
        if not display_ids:
            raise RuntimeError("No NSScreen instances found")
        return display_ids

    def _capture_loop(self, display_id: int) -> None:
        while not self._stop_event.is_set():
            start = time.monotonic()
            png_bytes = self._capture_png(display_id)
            if png_bytes:
                frame = CapturedImage(
                    display_id=display_id,
                    png_bytes=png_bytes,
                    timestamp=time.time(),
                    enqueued_monotonic=start,
                )
                try:
                    self._frame_handler(frame)
                    with self._lock:
                        self._last_emit[display_id] = start
                except Exception:  # pragma: no cover - defensive
                    logging.exception("Unhandled exception in frame handler")

            elapsed = time.monotonic() - start
            sleep_for = max(0.0, self._interval - elapsed)
            self._stop_event.wait(sleep_for)

    def _capture_png(self, display_id: int) -> Optional[bytes]:
        image = CGDisplayCreateImage(display_id)
        if image is None:
            logging.warning("ScreenshotCaptureManager: failed to capture display %s", display_id)
            return None

        data = CFDataCreateMutable(None, 0)
        destination = CGImageDestinationCreateWithData(data, "public.png", 1, None)
        if destination is None:
            logging.error("ScreenshotCaptureManager: failed to create image destination")
            return None

        CGImageDestinationAddImage(destination, image, None)
        if not CGImageDestinationFinalize(destination):
            logging.error(
                "ScreenshotCaptureManager: failed to finalize PNG for display %s",
                display_id,
            )
            return None

        return bytes(data)
