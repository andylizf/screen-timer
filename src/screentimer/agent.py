"""CLI entry point for the screen capture agent."""

import argparse
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import timedelta
from typing import Optional, Sequence

from screentimer.config import load_agent_config
from screentimer.policy import PolicyConfig, PolicyManager
from screentimer.processor import FrameProcessor, ProcessorOptions
from screentimer.streaming import ScreenCaptureManager
from screentimer.vlm import VLMClient


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the screen capture agent")
    parser.add_argument(
        "--frame-interval",
        type=float,
        default=0.5,
        help="Minimum frame interval in seconds per display (default: 0.5)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="File logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--console-level",
        default="INFO",
        help="Console logging level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to write logs (overrides SCREEN_TIMER_LOG_PATH)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_agent_config()

    log_path = config.log_path
    if args.log_file:
        log_path = Path(args.log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)

    class _SkipLiteLLMFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
            return not record.name.lower().startswith("litellm")

    file_level = getattr(logging, args.log_level.upper(), logging.INFO)
    console_level = getattr(logging, args.console_level.upper(), logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.addFilter(_SkipLiteLLMFilter())

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(file_level)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    for logger_name in list(logging.Logger.manager.loggerDict.keys()):
        if logger_name.lower().startswith("litellm"):
            logger = logging.getLogger(logger_name)
            logger.handlers.clear()
            logger.propagate = True

    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)

    vlm_client = VLMClient(model=config.vlm_model, prompt=config.vlm_prompt)
    policy_manager = PolicyManager(
        PolicyConfig(
            workday_cutoff=config.workday_cutoff,
            violation_grace=timedelta(seconds=config.violation_grace_seconds),
        )
    )
    processor = FrameProcessor(
        ProcessorOptions(
            log_interval=config.log_interval,
            sample_interval=config.sample_interval,
            capture_dir=config.capture_dir,
            queue_size=config.queue_size,
            vlm_client=vlm_client,
            policy_manager=policy_manager,
        )
    )

    manager = ScreenCaptureManager(
        processor.handle_frame,
        minimum_frame_interval=args.frame_interval,
        idle_callback=processor.handle_stream_idle,
    )

    stop_event = threading.Event()

    def _signal_handler(signum, _frame):
        logging.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        manager.start()
    except PermissionError as exc:
        logging.error("%s", exc)
        processor.shutdown()
        return 1

    logging.info("Screen capture agent is running")

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        manager.stop()
        processor.shutdown()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
