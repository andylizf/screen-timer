"""Frame processing pipeline: logging, persistence, and VLM dispatch."""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from screentimer.media import sample_buffer_to_png
from screentimer.streaming import CapturedFrame
from screentimer.vlm import VLMClient
from screentimer.policy import PolicyManager


@dataclass
class ProcessorOptions:
    log_interval: float
    sample_interval: float
    capture_dir: Optional[Path]
    queue_size: int
    vlm_client: VLMClient
    policy_manager: Optional[PolicyManager] = None


class FrameProcessor:
    """Consumes captured frames and performs logging / inference."""

    def __init__(self, options: ProcessorOptions) -> None:
        self._options = options
        self._stats: Dict[int, int] = {}
        self._stats_lock = threading.Lock()
        self._last_log = time.monotonic()
        self._last_sample: Dict[int, float] = {}
        self._task_queue: queue.Queue[CapturedFrame] = queue.Queue(
            maxsize=options.queue_size
        )
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop, name="frame-processor", daemon=True
        )
        self._worker.start()

    def handle_frame(self, frame: CapturedFrame) -> None:
        """Entry point passed to `ScreenCaptureManager`."""

        self._record_stats(frame)

        if self._options.sample_interval <= 0:
            return

        last_ts = self._last_sample.get(frame.display_id, 0.0)
        if frame.timestamp - last_ts < self._options.sample_interval:
            return

        self._last_sample[frame.display_id] = frame.timestamp
        try:
            self._task_queue.put_nowait(frame)
        except queue.Full:  # pragma: no cover
            logging.warning(
                "Frame queue full; dropping frame for display %s", frame.display_id
            )

    def shutdown(self) -> None:
        """Stop background workers and flush queues."""

        self._stop_event.set()
        self._worker.join(timeout=5)
        while not self._task_queue.empty():
            try:
                self._task_queue.get_nowait()
                self._task_queue.task_done()
            except queue.Empty:  # pragma: no cover
                break

    def handle_stream_idle(self, display_id: int, idle_seconds: float) -> None:
        policy = self._options.policy_manager
        if policy is not None:
            policy.handle_stream_idle(display_id, idle_seconds)

    # Internal helpers -------------------------------------------------

    def _record_stats(self, frame: CapturedFrame) -> None:
        with self._stats_lock:
            self._stats[frame.display_id] = self._stats.get(frame.display_id, 0) + 1
            now = time.monotonic()
            if now - self._last_log >= self._options.log_interval:
                for display_id, count in self._stats.items():
                    logging.info(
                        "Display %s: %s frames captured (ts=%.3f)",
                        display_id,
                        count,
                        frame.timestamp,
                    )
                self._stats.clear()
                self._last_log = now

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._process_sample(frame)
            finally:
                self._task_queue.task_done()

    def _process_sample(self, frame: CapturedFrame) -> None:
        processing_started = time.monotonic()
        queue_delay = processing_started - getattr(frame, "received_monotonic", processing_started)
        logging.debug(
            "FrameProcessor: display %s queue delay %.3fs",
            frame.display_id,
            queue_delay,
        )

        png_bytes = sample_buffer_to_png(frame.sample_buffer)
        if not png_bytes:
            return

        capture_dir = self._options.capture_dir
        if capture_dir is not None:
            self._save_thumbnail(capture_dir, frame, png_bytes)

        if self._options.vlm_client.enabled:
            vlm_started = time.monotonic()
            result = self._options.vlm_client.classify(png_bytes)
            vlm_duration = time.monotonic() - vlm_started
            logging.debug(
                "FrameProcessor: display %s VLM latency %.3fs",
                frame.display_id,
                vlm_duration,
            )
            if result:
                logging.info("Display %s VLM result: %s", frame.display_id, result)
                if self._options.policy_manager is not None:
                    self._options.policy_manager.handle_frame_result(
                        frame.display_id,
                        result,
                        timestamp=frame.timestamp,
                    )

    def _save_thumbnail(self, capture_dir: Path, frame: CapturedFrame, png_bytes: bytes) -> None:
        timestamp_ms = int(frame.timestamp * 1000)
        filename = f"display-{frame.display_id}-{timestamp_ms}.png"
        path = capture_dir / filename
        try:
            path.write_bytes(png_bytes)
        except Exception:  # pragma: no cover
            logging.exception("Failed to write frame thumbnail to %s", path)
