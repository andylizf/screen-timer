"""Periodic screenshot capture using the macOS `screencapture` utility."""

import logging
import os
import subprocess
import threading
import time
from tempfile import NamedTemporaryFile
from typing import Callable, Dict, List, Optional, Tuple

from AppKit import NSScreen  # type: ignore

from screentimer.processor import CapturedImage

_REFRESH_BACKOFF_SECONDS = 2.0


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
        self._last_refresh: float = 0.0

    # Public API -------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        if not self._refresh_displays(initial=True):
            raise RuntimeError("No displays available for screenshot capture")

        self._worker = threading.Thread(
            target=self._run_capture_loop,
            name="screenshot-capture",
            daemon=True,
        )
        self._worker.start()

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
            self._last_refresh = time.monotonic()
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
        return indices

    def _refresh_displays(self, *, initial: bool = False) -> bool:
        indices = self._enumerate_displays()
        now = time.monotonic()
        with self._lock:
            old_set = set(self._display_ids)
            new_ids = sorted(indices.keys())
            new_set = set(new_ids)
            added = new_set - old_set
            removed = old_set - new_set

            self._display_ids = new_ids
            self._display_indices = indices
            for display_id in removed:
                self._display_intervals.pop(display_id, None)
                self._last_capture.pop(display_id, None)
                logging.info(
                    "ScreenshotCaptureManager: removed display %s",
                    display_id,
                )
            for display_id in added:
                self._display_intervals[display_id] = self._base_interval
                self._last_capture[display_id] = now - self._base_interval
                logging.info(
                    "ScreenshotCaptureManager: detected display %s (interval %.1fs)",
                    display_id,
                    self._base_interval,
                )
            self._last_refresh = now

        if not indices:
            if initial:
                return False
            logging.warning("ScreenshotCaptureManager: no displays currently available")
            return False
        return True

    def _run_capture_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                display_ids = list(self._display_ids)
                intervals = self._display_intervals.copy()
                last_capture = self._last_capture.copy()
                last_refresh = self._last_refresh

            if not display_ids:
                if time.monotonic() - last_refresh >= _REFRESH_BACKOFF_SECONDS:
                    self._refresh_displays()
                self._stop_event.wait(1.0)
                continue

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
                png_bytes, error = self._capture_png(display_id)
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
                elif error:
                    self._handle_capture_error(display_id, error)

                with self._lock:
                    if display_id in self._last_capture:
                        self._last_capture[display_id] = capture_end

    def _capture_png(self, display_id: int) -> Tuple[Optional[bytes], Optional[str]]:
        with self._lock:
            index = self._display_indices.get(display_id)
        if index is None:
            return None, "missing display index"

        tmp_path = None
        try:
            tmp = NamedTemporaryFile(suffix=".png", delete=False)
            tmp_path = tmp.name
            tmp.close()

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
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="ignore") or result.stdout.decode(
                    "utf-8", errors="ignore"
                )
                return None, error or f"screencapture exited with {result.returncode}"

            try:
                with open(tmp_path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:  # pragma: no cover
                return None, str(exc)

            if not data:
                return None, "empty screenshot"
            return data, None
        except Exception as exc:  # pragma: no cover
            return None, str(exc)
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    logging.debug(
                        "ScreenshotCaptureManager: failed to remove temp file %s",
                        tmp_path,
                    )

    def _handle_capture_error(self, display_id: int, error: str) -> None:
        message = (error or "").strip()
        logging.warning(
            "ScreenshotCaptureManager: capture error on display %s: %s",
            display_id,
            message or "unknown error",
        )

        lower = message.lower()
        should_refresh = False
        if "invalid display" in lower or "only" in lower and "valid value" in lower:
            should_refresh = True
        elif "missing display index" in lower:
            should_refresh = True
        elif "no displays" in lower:
            should_refresh = True

        if should_refresh and time.monotonic() - self._last_refresh >= 0.5:
            logging.info("ScreenshotCaptureManager: refreshing display topology after failure")
            self._refresh_displays()

    # Interval control ------------------------------------------------

    def tighten_interval(self, display_id: int, interval: float) -> None:
        if interval <= 0:
            return
        with self._lock:
            if display_id not in self._display_intervals:
                return
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
            if display_id not in self._display_intervals:
                return
            if self._display_intervals.get(display_id, self._base_interval) == self._base_interval:
                return
            self._display_intervals[display_id] = self._base_interval
        logging.info(
            "ScreenshotCaptureManager: restored interval for display %s to %.1fs",
            display_id,
            self._base_interval,
        )
