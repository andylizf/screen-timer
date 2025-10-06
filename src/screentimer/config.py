"""Configuration helpers for the screen timer agent."""

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class AgentConfig:
    """Holds runtime options for the capture agent."""

    log_interval: float = 5.0
    sample_interval: float = 10.0
    capture_dir: Optional[Path] = None
    queue_size: int = 16
    vlm_model: Optional[str] = None
    vlm_prompt: str = (
        "Classify whether the captured macOS screen content shows entertainment or work. "
        'Respond with a short JSON object like {"label": "entertainment", "confidence": 0.8} and '
        "include a brief reason."
    )
    log_path: Path = Path("logs/screen-timer.log")
    workday_cutoff: time = time(17, 0)
    violation_grace_seconds: int = 30
    capture_interval: float = 20.0
    violation_capture_interval: Optional[float] = 5.0
    reminder_interval_seconds: int = 10


def load_agent_config() -> AgentConfig:
    """Load configuration from environment variables (optionally via .env)."""

    capture_dir_env = os.getenv("SCREEN_TIMER_CAPTURE_DIR")
    capture_dir = Path(capture_dir_env).expanduser() if capture_dir_env else None
    if capture_dir is not None:
        capture_dir.mkdir(parents=True, exist_ok=True)

    log_interval = float(os.getenv("SCREEN_TIMER_LOG_INTERVAL", "5"))
    sample_interval = float(os.getenv("SCREEN_TIMER_SAMPLE_INTERVAL", "10"))
    queue_size = int(os.getenv("SCREEN_TIMER_QUEUE_SIZE", "16"))
    capture_interval = float(
        os.getenv("SCREEN_TIMER_CAPTURE_INTERVAL", str(AgentConfig.capture_interval))
    )
    violation_capture_interval_env = os.getenv("SCREEN_TIMER_VIOLATION_CAPTURE_INTERVAL")
    if violation_capture_interval_env is not None and violation_capture_interval_env.strip():
        violation_capture_interval = float(violation_capture_interval_env)
    else:
        violation_capture_interval = AgentConfig.violation_capture_interval
    reminder_interval_seconds = int(
        os.getenv("SCREEN_TIMER_REMINDER_INTERVAL", str(AgentConfig.reminder_interval_seconds))
    )
    vlm_model = os.getenv("SCREEN_TIMER_VLM_MODEL")
    prompt = os.getenv("SCREEN_TIMER_VLM_PROMPT")
    log_path_env = os.getenv("SCREEN_TIMER_LOG_PATH")
    if log_path_env:
        log_path = Path(log_path_env).expanduser()
    else:
        log_path = AgentConfig.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cutoff_env = os.getenv("SCREEN_TIMER_WORKDAY_CUTOFF")
    if cutoff_env:
        hours, minutes = cutoff_env.split(":") if ":" in cutoff_env else (cutoff_env, "0")
        workday_cutoff = time(int(hours), int(minutes))
    else:
        workday_cutoff = AgentConfig.workday_cutoff

    violation_grace = int(os.getenv("SCREEN_TIMER_VIOLATION_GRACE", str(AgentConfig.violation_grace_seconds)))

    return AgentConfig(
        log_interval=log_interval,
        sample_interval=sample_interval,
        capture_dir=capture_dir,
        queue_size=queue_size,
        vlm_model=vlm_model,
        vlm_prompt=prompt or AgentConfig.vlm_prompt,
        log_path=log_path,
        workday_cutoff=workday_cutoff,
        violation_grace_seconds=violation_grace,
        capture_interval=capture_interval,
        violation_capture_interval=violation_capture_interval,
        reminder_interval_seconds=reminder_interval_seconds,
    )
