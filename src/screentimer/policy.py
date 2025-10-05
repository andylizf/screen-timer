"""Simple policy manager for enforcing work-hour rules."""

import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Optional, Tuple

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


@dataclass
class PolicyConfig:
    workday_cutoff: time
    violation_grace: timedelta
    notification_title: str = "Screen Timer"
    lock_cmd: Tuple[str, ...] = (
        "osascript",
        "-e",
        'tell application "System Events" to keystroke "q" using {control down, command down}',
    )


@dataclass
class _ViolationState:
    first_detected: datetime
    last_notified: datetime
    notified: bool = False


class PolicyManager:
    """Tracks classifications and triggers interventions when needed."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._violations: Dict[int, _ViolationState] = {}
        self._last_label: Dict[int, Tuple[str, float]] = {}
        self._use_terminal_notifier = shutil.which("terminal-notifier") is not None

    def handle_frame_result(self, display_id: int, result_text: str, *, timestamp: float) -> None:
        parsed = _parse_result(result_text)
        if parsed is None:
            logging.debug("PolicyManager: unable to parse result for display %s", display_id)
            return

        label = parsed.get("label", "").lower()
        confidence = float(parsed.get("confidence", 0)) if parsed.get("confidence") is not None else 0.0
        now = datetime.now()

        self._last_label[display_id] = (label, confidence)

        if label != "entertainment" or confidence < 0.6 or not _within_work_hours(now, self._config.workday_cutoff):
            if display_id in self._violations:
                logging.info("PolicyManager: resetting violation for display %s", display_id)
                self._violations.pop(display_id, None)
            self._last_label.pop(display_id, None)
            return

        state = self._violations.get(display_id)
        if state is None:
            logging.info(
                "PolicyManager: entertainment detected on display %s (confidence %.2f), issuing warning",
                display_id,
                confidence,
            )
            self._send_notification(display_id, "Entertainment detected; please return to work")
            self._violations[display_id] = _ViolationState(first_detected=now, last_notified=now, notified=True)
            return

        elapsed = now - state.first_detected
        if elapsed >= self._config.violation_grace:
            logging.warning(
                "PolicyManager: display %s exceeded grace period (%.1fs), locking screen",
                display_id,
                elapsed.total_seconds(),
            )
            self._lock_screen()
            self._violations.pop(display_id, None)
        elif (now - state.last_notified) >= timedelta(seconds=10):
            # Remind periodically
            logging.info(
                "PolicyManager: repeated entertainment on display %s; sending reminder",
                display_id,
            )
            self._send_notification(display_id, "Entertainment still detected; lock imminent")
            state.last_notified = now

    # ------------------------------------------------------------------

    def handle_stream_idle(self, display_id: int, idle_seconds: float) -> None:
        now = datetime.now()
        state = self._violations.get(display_id)
        if state is None:
            return

        label_info = self._last_label.get(display_id)
        confidence = label_info[1] if label_info else 0.0
        logging.info(
            "PolicyManager: display %s idle for %.1fs but violation active (confidence %.2f)",
            display_id,
            idle_seconds,
            confidence,
        )

        elapsed = now - state.first_detected
        if elapsed >= self._config.violation_grace:
            logging.warning(
                "PolicyManager: idle entertainment on display %s exceeded grace period (%.1fs), locking screen",
                display_id,
                elapsed.total_seconds(),
            )
            self._lock_screen()
            self._violations.pop(display_id, None)
            return

        if (now - state.last_notified) >= timedelta(seconds=10):
            self._send_notification(
                display_id,
                f"Entertainment still detected (idle {idle_seconds:.0f}s); lock after {int(self._config.violation_grace.total_seconds() - elapsed.total_seconds())}s",
            )
            state.last_notified = now

    def _send_notification(self, display_id: int, message: str) -> None:
        try:
            if self._use_terminal_notifier:
                cmd = [
                    "terminal-notifier",
                    "-sender",
                    "com.apple.Terminal",
                    "-title",
                    self._config.notification_title,
                    "-subtitle",
                    f"Display {display_id}",
                    "-message",
                    message,
                ]
                subprocess.run(cmd, check=True)
            else:
                script = (
                    f'display notification "{message}" with title "{self._config.notification_title}" '
                    f'subtitle "Display {display_id}"'
                )
                subprocess.run(["osascript", "-e", script], check=True)
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            logging.error("Notification command failed: %s", exc)
        except FileNotFoundError:  # pragma: no cover
            logging.error("Notification tool not found for alerts")

    def _lock_screen(self) -> None:
        try:
            subprocess.run(self._config.lock_cmd, check=True)
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            logging.error("Lock command failed: %s", exc)
        except FileNotFoundError:  # pragma: no cover
            logging.error("Lock command not found: %s", self._config.lock_cmd)


# ----------------------------------------------------------------------


def _parse_result(text: str) -> Optional[dict]:
    """Extract JSON object from the model response."""

    text = text.strip()
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidate = match.group(1)
    else:
        candidate = text

    # drop trailing explanations such as "Reason: ..."
    candidate = candidate.split("Reason:", 1)[0].strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON object manually
    brace_index = candidate.find("{")
    if brace_index == -1:
        return None
    json_candidate = candidate[brace_index:]
    json_candidate = json_candidate.split("Reason:", 1)[0].strip()
    try:
        return json.loads(json_candidate)
    except json.JSONDecodeError:
        logging.debug("PolicyManager: failed to parse JSON from result: %s", candidate)
        return None


def _within_work_hours(now: datetime, cutoff: time) -> bool:
    current_time = now.time()
    return current_time < cutoff
