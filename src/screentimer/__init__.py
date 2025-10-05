"""Screen timer core package."""

__all__ = [
    "CapturedFrame",
    "ScreenCaptureManager",
    "ensure_screen_recording_permission",
    "FrameProcessor",
    "load_agent_config",
]

from .config import load_agent_config
from .permissions import ensure_screen_recording_permission
from .processor import FrameProcessor
from .streaming import CapturedFrame, ScreenCaptureManager
