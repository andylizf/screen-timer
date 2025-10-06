"""Periodic screenshot capture using the macOS `screencapture` utility."""

import logging
import os
import subprocess
import threading
import time
from tempfile import NamedTemporaryFile
from typing import Callable, Dict, List, Optional

from AppKit import NSScreen  # type: ignore

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
        self._base_interval = capture_interval
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._display_intervals: Dict[int, float] = {}
        self._last_capture: Dict[int, float] = {}
        self._display_ids: List[int] = []
        self._display_indices: Dict[int, int] = {}
        self._worker: Optional[threading.Thread] = None

    # Public API -------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        display_indices = self._enumerate_displays()
        if not display_indices:
            raise RuntimeError("No displays available for screenshot capture")
        display_ids = list(display_indices.keys())

        now = time.monotonic()
        with self._lock:
            self._display_ids = display_ids
            self._display_indices = display_indices
            self._display_intervals = {display_id: self._base_interval for display_id in display_ids}
            self._last_capture = {display_id: now - self._base_interval for display_id in display_ids}

        self._worker = threading.Thread(
            target=self._run_capture_loop,
            name="screenshot-capture",
            daemon=True,
        )
        self._worker.start()

        for display_id in display_ids:
            logging.info(
                "ScreenshotCaptureManager started for display %s (interval %.1fs)",
                display_id,
                self._base_interval,
            )

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=self._base_interval + 1.0)
            self._worker = None
        with self._lock:
            self._display_ids.clear()
            self._display_indices.clear()
            self._display_intervals.clear()
            self._last_capture.clear()
        logging.info("ScreenshotCaptureManager stopped")

    # Internal helpers -------------------------------------------------

    def _enumerate_displays(self) -> Dict[int, int]:
        screens = NSScreen.screens()
        indices: Dict[int, int] = {}
        for idx, screen in enumerate(screens or [], start=1):
            desc = screen.deviceDescription()
            display_number = desc.get("NSScreenNumber") if desc is not None else None
            if display_number is not None:
                indices[int(display_number)] = idx
        if not indices:
            raise RuntimeError("No NSScreen instances found")
        return indices

    def _run_capture_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                display_ids = list(self._display_ids)
                intervals = self._display_intervals.copy()
                last_capture = self._last_capture.copy()

            now = time.monotonic()
            due_displays: List[int] = []
            next_wait = self._base_interval

            for display_id in display_ids:
                interval = intervals.get(display_id, self._base_interval)
                last_time = last_capture.get(display_id, 0.0)
                elapsed = now - last_time
                if elapsed >= interval:
                    due_displays.append(display_id)
                else:
                    remaining = interval - elapsed
                    if remaining < next_wait:
                        next_wait = remaining

            if not due_displays:
                self._stop_event.wait(max(0.1, next_wait))
                continue

            for display_id in due_displays:
                if self._stop_event.is_set():
                    break
                capture_start = time.monotonic()
                png_bytes = self._capture_png(display_id)
                capture_end = time.monotonic()
                if png_bytes:
                    frame = CapturedImage(
                        display_id=display_id,
                        png_bytes=png_bytes,
                        timestamp=time.time(),
                        enqueued_monotonic=capture_start,
                    )
                    try:
                        self._frame_handler(frame)
                    except Exception:  # pragma: no cover - defensive
                        logging.exception("Unhandled exception in frame handler")
                with self._lock:
                    self._last_capture[display_id] = capture_end

    def _capture_png(self, display_id: int) -> Optional[bytes]:
        with self._lock:
            index = self._display_indices.get(display_id)
        if index is None:
            logging.warning(
                "ScreenshotCaptureManager: no display index for display %s", display_id
            )
            return None

        with NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            result = subprocess.run(
                [
                    "screencapture",
                    "-x",
                    "-t",
                    "png",
                    "-D",
                    str(index),
                    tmp_path,
                ],
                check=True,
                capture_output=True,
            )
            if result.returncode != 0:
                logging.error(
                    "ScreenshotCaptureManager: screencapture exited with %s for display %s",
                    result.returncode,
                    display_id,
                )
                return None
            try:
                with open(tmp_path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:  # pragma: no cover
                logging.error(
                    "ScreenshotCaptureManager: failed to read screenshot for display %s: %s",
                    display_id,
                    exc,
                )
                return None
            if not data:
                logging.warning(
                    "ScreenshotCaptureManager: empty screenshot for display %s", display_id
                )
                return None
            return data
        except subprocess.CalledProcessError as exc:
            logging.error(
                "ScreenshotCaptureManager: screencapture failed for display %s: %s",
                display_id,
                exc.stderr.decode("utf-8", errors="ignore"),
            )
            return None
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
            except Exception:  # pragma: no cover
                logging.debug("ScreenshotCaptureManager: failed to remove temp file %s", tmp_path)

    # Interval control ------------------------------------------------

    def tighten_interval(self, display_id: int, interval: float) -> None:
        if interval <= 0:
            return
        with self._lock:
            current = self._display_intervals.get(display_id, self._base_interval)
            if current == interval:
                return
            self._display_intervals[display_id] = interval
        logging.info(
            "ScreenshotCaptureManager: tightened interval for display %s to %.1fs",
            display_id,
            interval,
        )

    def restore_interval(self, display_id: int) -> None:
        with self._lock:
            if self._display_intervals.get(display_id, self._base_interval) == self._base_interval:
                return
            self._display_intervals[display_id] = self._base_interval
        logging.info(
            "ScreenshotCaptureManager: restored interval for display %s to %.1fs",
            display_id,
            self._base_interval,
        )
