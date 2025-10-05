"""Screen capture manager built on top of ScreenCaptureKit via PyObjC."""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, cast

import objc
from Cocoa import NSObject  # type: ignore
from CoreFoundation import (  # type: ignore
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
)

import CoreMedia  # type: ignore

try:  # pragma: no cover - optional but recommended
    import dispatch  # type: ignore
except ImportError:  # pragma: no cover
    dispatch = None

from screentimer.permissions import ensure_screen_recording_permission

SCStreamOutputProtocol = objc.protocolNamed("SCStreamOutput")
SCStreamDelegateProtocol = objc.protocolNamed("SCStreamDelegate")

try:
    from ScreenCaptureKit import (  # type: ignore
        SCCaptureResolutionBest,
        SCContentFilter,
        SCShareableContent,
        SCStream,
        SCStreamConfiguration,
        SCStreamOutputTypeScreen,
    )
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "ScreenCaptureKit bindings not found. Install pyobjc-framework-ScreenCaptureKit."
    ) from exc


@dataclass
class CapturedFrame:
    """Container for frames emitted by ScreenCaptureKit."""

    display_id: int
    sample_buffer: object
    timestamp: float
    received_monotonic: float

    @property
    def pixel_buffer(self):
        """Return the CVPixelBuffer backing this frame."""
        return CoreMedia.CMSampleBufferGetImageBuffer(self.sample_buffer)


class _FrameStreamDelegate(
    NSObject, protocols=[SCStreamOutputProtocol, SCStreamDelegateProtocol]
):  # pyright: ignore[misc]
    """Receives SCStream sample buffers and forwards them to the manager."""

    manager = objc.ivar()
    display_id = objc.ivar("I")

    def initWithManager_displayID_(self, manager, display_id):  # noqa: N802
        self = objc.super(_FrameStreamDelegate, self).init()
        if self is None:
            return None
        self.manager = manager
        self.display_id = int(display_id)
        return self

    # pylint: disable=unused-argument
    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):  # noqa: N802
        self.manager.handle_sample_buffer(self.display_id, sample_buffer, output_type)

    def stream_didStopWithError_(self, stream, error):  # noqa: N802
        self.manager.handle_stream_stop(self.display_id, error)


class ScreenCaptureManager:
    """Manages ScreenCaptureKit streams for connected displays."""

    def __init__(
        self,
        frame_handler: Callable[[CapturedFrame], None],
        *,
        minimum_frame_interval: float = 0.2,
        queue_depth: int = 3,
        shows_cursor: bool = False,
        idle_callback: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self._frame_handler = frame_handler
        self._min_interval = minimum_frame_interval
        self._queue_depth = queue_depth
        self._shows_cursor = shows_cursor
        self._streams: Dict[int, SCStream] = {}
        self._delegates: Dict[int, _FrameStreamDelegate] = {}
        self._queues: Dict[int, object] = {}
        self._displays: Dict[int, Any] = {}
        self._last_frame: Dict[int, float] = {}
        self._idle_callback = idle_callback
        self._idle_notified: Dict[int, bool] = {}
        self._last_restart: Dict[int, float] = {}
        self._streams_lock = threading.Lock()
        self._running = False
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_window = 20.0
        self._watchdog_check_interval = 5.0
        self._restart_backoff = 30.0

    # Public API -------------------------------------------------------

    def start(self) -> None:
        if self._running:
            logging.debug("ScreenCaptureManager already running")
            return

        if not ensure_screen_recording_permission(prompt=True):
            raise PermissionError("Screen recording permission denied by the user")

        content = self._get_shareable_content()
        displays = list(cast(Any, content).displays()) if content is not None else []
        if not displays:
            raise RuntimeError("No displays available for capture")

        for display in displays:
            config = self._make_configuration(display)
            self._start_stream_for_display(display, config)

        self._running = True
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="sc-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        logging.info("ScreenCaptureManager started for %d displays", len(displays))

    def stop(self) -> None:
        with self._streams_lock:
            for display_id, stream in list(self._streams.items()):
                self._stop_stream(display_id, stream)
            self._streams.clear()
            self._delegates.clear()
            self._queues.clear()
            self._displays.clear()
            self._last_frame.clear()
            self._idle_notified.clear()
            self._last_restart.clear()
        self._running = False
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=5)
            self._watchdog_thread = None
        logging.info("ScreenCaptureManager stopped")

    # Internal helpers -------------------------------------------------

    def _get_shareable_content(self) -> Any:
        event = threading.Event()
        state: Dict[str, Any] = {}

        def handler(content, error):
            state["content"] = content
            state["error"] = error
            event.set()

        SCShareableContent.getShareableContentWithCompletionHandler_(handler)
        while not event.is_set():
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)

        error = state.get("error")
        if error is not None:
            raise RuntimeError(f"Failed to enumerate displays: {error}")

        return state.get("content")

    def _make_configuration(self, display: Any) -> SCStreamConfiguration:
        config = cast(SCStreamConfiguration, SCStreamConfiguration.alloc().init())
        config.setQueueDepth_(self._queue_depth)
        config.setCaptureResolution_(SCCaptureResolutionBest)
        config.setShowsCursor_(self._shows_cursor)
        if self._min_interval > 0:
            timescale = 600
            value = max(1, int(self._min_interval * timescale))
            config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(value, timescale))
        if hasattr(display, "width") and hasattr(display, "height"):
            try:
                width = (
                    int(display.width())
                    if callable(display.width)
                    else int(display.width)
                )
                height = (
                    int(display.height())
                    if callable(display.height)
                    else int(display.height)
                )
                if width > 0 and height > 0:
                    config.setWidth_(width)
                    config.setHeight_(height)
            except Exception:  # pragma: no cover
                logging.debug("Failed to derive display dimensions for %s", display)
        return config

    def _start_stream_for_display(self, display: Any, configuration: SCStreamConfiguration):
        display_obj = cast(Any, display)
        display_id = (
            int(display_obj.displayID()) if hasattr(display_obj, "displayID") else id(display_obj)
        )
        display_info = self._summarize_display(display_obj, display_id)
        logging.info("Opening stream for display %s", display_info)

        filter_ = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            display_obj,
            [],
            [],
        )

        delegate = _FrameStreamDelegate.alloc().initWithManager_displayID_(
            self, display_id
        )
        delegate = cast(_FrameStreamDelegate, delegate)
        queue = None
        if dispatch is not None:
            queue = dispatch.dispatch_queue_create(
                f"screen-timer.display.{display_id}", None
            )
            self._queues[display_id] = queue

        stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            filter_, configuration, delegate
        )
        stream = cast(SCStream, stream)

        success, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            delegate,
            SCStreamOutputTypeScreen,
            queue,
            None,
        )
        if not success:
            raise RuntimeError(f"Failed to add screen stream output: {error}")

        def start_handler(start_error):
            if start_error is not None:
                logging.error(
                    "Failed to start stream for display %s: %s", display_id, start_error
                )

        stream.startCaptureWithCompletionHandler_(start_handler)

        with self._streams_lock:
            self._streams[display_id] = stream
            self._delegates[display_id] = delegate
            self._displays[display_id] = display_obj
            self._last_frame[display_id] = time.monotonic()
            self._idle_notified[display_id] = False
            self._last_restart[display_id] = time.monotonic()

    def _stop_stream(self, display_id: int, stream: SCStream) -> None:
        def stop_handler(error):
            if error is not None:
                logging.error("Error stopping stream %s: %s", display_id, error)

        stream.stopCaptureWithCompletionHandler_(stop_handler)

    def _maybe_restart_stream(self, display_id: int, idle_duration: float) -> None:
        """Restart a display stream if enough time has passed since the last attempt."""

        now = time.monotonic()
        last_restart = self._last_restart.get(display_id, 0.0)
        if now - last_restart < self._restart_backoff:
            logging.debug(
                "Watchdog: restart for display %s suppressed (%.1fs since last attempt)",
                display_id,
                now - last_restart,
            )
            return

        with self._streams_lock:
            display_obj = self._displays.get(display_id)
            stream = self._streams.pop(display_id, None)
            if display_obj is None:
                logging.debug(
                    "Watchdog: no display object retained for %s; cannot restart",
                    display_id,
                )
                return
            self._delegates.pop(display_id, None)
            self._queues.pop(display_id, None)
            # reset idle tracking so the watchdog does not immediately re-trigger
            self._last_restart[display_id] = now
            self._last_frame[display_id] = now
            self._idle_notified[display_id] = False

        if stream is not None:
            try:
                self._stop_stream(display_id, stream)
            except Exception:  # pragma: no cover
                logging.exception("Watchdog: failed to stop stream %s for restart", display_id)

        configuration = self._make_configuration(display_obj)
        try:
            self._start_stream_for_display(display_obj, configuration)
        except Exception:  # pragma: no cover
            logging.exception(
                "Watchdog: failed to restart stream for display %s after %.1fs idle",
                display_id,
                idle_duration,
            )
        else:
            logging.info(
                "Watchdog: restarted stream for display %s after %.1fs idle",
                display_id,
                idle_duration,
            )

    def _summarize_display(self, display_obj: Any, display_id: int) -> str:
        """Return a human-friendly string describing a display."""

        def _maybe_call(obj, attr: str):
            value = getattr(obj, attr, None)
            if callable(value):
                try:
                    return value()
                except Exception:  # pragma: no cover - best-effort metadata
                    return None
            return value

        width = _maybe_call(display_obj, "width")
        height = _maybe_call(display_obj, "height")
        pixel_width = _maybe_call(display_obj, "pixelWidth")
        pixel_height = _maybe_call(display_obj, "pixelHeight")
        uuid = _maybe_call(display_obj, "displayUUID")

        parts = [f"id={display_id}"]
        if width and height:
            parts.append(f"logical={width}x{height}")
        if pixel_width and pixel_height:
            parts.append(f"native={pixel_width}x{pixel_height}")
        if uuid:
            parts.append(f"uuid={uuid}")
        return ", ".join(parts)

    # Delegate callbacks -----------------------------------------------

    def handle_sample_buffer(self, display_id: int, sample_buffer, output_type) -> None:
        if output_type != SCStreamOutputTypeScreen:
            return

        if not CoreMedia.CMSampleBufferIsValid(sample_buffer):
            return

        self._last_frame[display_id] = time.monotonic()
        self._idle_notified[display_id] = False

        timestamp = CoreMedia.CMTimeGetSeconds(
            CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample_buffer)
        )
        frame = CapturedFrame(
            display_id=display_id,
            sample_buffer=sample_buffer,
            timestamp=timestamp,
            received_monotonic=time.monotonic(),
        )
        try:
            self._frame_handler(frame)
        except Exception:  # pragma: no cover
            logging.exception("Unhandled exception in frame handler")

    def handle_stream_stop(self, display_id: int, error) -> None:
        if error is not None:
            logging.error("Stream %s stopped with error: %s", display_id, error)
        else:
            logging.info("Stream %s stopped", display_id)
        with self._streams_lock:
            self._streams.pop(display_id, None)
            self._delegates.pop(display_id, None)
            self._queues.pop(display_id, None)
            self._displays.pop(display_id, None)
            self._last_frame.pop(display_id, None)
            self._idle_notified.pop(display_id, None)
            self._last_restart.pop(display_id, None)

    # Watchdog ---------------------------------------------------------

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_check_interval):
            now = time.monotonic()
            for display_id, last_time in list(self._last_frame.items()):
                idle_duration = now - last_time
                if idle_duration > self._watchdog_window:
                    if not self._idle_notified.get(display_id, False):
                        logging.warning(
                            "Watchdog: display %s idle for %.1fs",
                            display_id,
                            idle_duration,
                        )
                        self._idle_notified[display_id] = True
                    self._maybe_restart_stream(display_id, idle_duration)
                    if self._idle_callback is not None:
                        try:
                            self._idle_callback(display_id, idle_duration)
                        except Exception:  # pragma: no cover
                            logging.exception(
                                "Idle callback failed for display %s", display_id
                            )
                else:
                    self._idle_notified[display_id] = False
