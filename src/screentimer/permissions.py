"""Screen recording permission helpers for macOS."""

import logging

try:
    import Quartz  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Quartz (pyobjc-framework-Quartz) is required. Install pyobjc first."
    ) from exc


def ensure_screen_recording_permission(*, prompt: bool = True) -> bool:
    """Ensure the process has screen recording permission.

    Parameters
    ----------
    prompt:
        When True (default), the system permission dialog is triggered if the process
        is not currently authorized. When False, the function simply reports the
        current status.

    Returns
    -------
    bool
        True when authorization is present (or granted after prompting), False if
        the user declined or the platform does not support runtime prompts.
    """

    granted = Quartz.CGPreflightScreenCaptureAccess()
    if granted or not prompt:
        return bool(granted)

    logging.info("Requesting macOS screen recording permission")
    return bool(Quartz.CGRequestScreenCaptureAccess())
