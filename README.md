# Screen Timer (prototype)

Python-based agent that periodically screenshots all connected displays (default every 20 seconds), persists sampled thumbnails, and forwards them to a vision-language model (VLM) for activity classification.

## Prerequisites
- macOS 12.3 or newer (macOS 15.4 introduces weekly screen-recording reauthorization: be ready to re-prompt users).
- Python 3.10.
- [uv](https://github.com/astral-sh/uv) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Screen Recording permission (System Settings → Privacy & Security → Screen & System Audio Recording).
- [`terminal-notifier`](https://github.com/julienXX/terminal-notifier) for native macOS alerts (install via `brew install terminal-notifier`). Without it the agent falls back to a plain AppleScript notification.

## Setup (uv)
```bash
uv init --name screen-timer --package
uv add pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-AppKit \
       pyobjc-framework-Quartz pyobjc-framework-CoreMedia \
       pyobjc-framework-CoreImage \
       python-dotenv Pillow litellm
uv sync
uv run pip install -e .
```

### Configure environment
Create `.env` in the project root (values are examples):
```env
OPENAI_API_KEY=sk-...
SCREEN_TIMER_CAPTURE_DIR=./captures
SCREEN_TIMER_SAMPLE_INTERVAL=10
SCREEN_TIMER_CAPTURE_INTERVAL=20
SCREEN_TIMER_LOG_INTERVAL=5
SCREEN_TIMER_VLM_MODEL=gpt-4o
SCREEN_TIMER_VLM_PROMPT=Classify whether this macOS screenshot is entertainment or work; respond with JSON.
SCREEN_TIMER_LOG_PATH=./logs/screen-timer.log
SCREEN_TIMER_WORKDAY_CUTOFF=17:00
SCREEN_TIMER_VIOLATION_GRACE=30
```
- Omit `SCREEN_TIMER_CAPTURE_DIR` to disable thumbnail export.
- Omit `SCREEN_TIMER_VLM_MODEL` to run without VLM inference.
- `SCREEN_TIMER_LOG_PATH` controls where logs are persisted (defaults to `logs/screen-timer.log`).
- `SCREEN_TIMER_WORKDAY_CUTOFF` sets the latest time (local) when entertainment is still blocked; default 17:00.
- `SCREEN_TIMER_VIOLATION_GRACE` defines how many seconds of persistent entertainment trigger a lock (default 30 seconds).
- `SCREEN_TIMER_CAPTURE_INTERVAL` sets the screenshot cadence in seconds (default 20).

## Running the capture agent
```bash
uv run screen-timer-agent --capture-interval 20 --log-level DEBUG --console-level INFO
uv run screen-timer-agent --log-file ~/screen-timer.log  # override log path
```
- The first launch triggers a macOS prompt via `CGRequestScreenCaptureAccess()`.
- The agent logs per-display capture counts, writes sampled PNG thumbnails (if a capture directory is configured), and sends the same screenshots to the configured VLM through `litellm`.
- During work hours (before the cutoff), sustained "entertainment" classifications trigger macOS notifications; if the user does not stop within the grace period the agent issues Control+Command+Q to lock the screen.
- Use `Ctrl+C` to stop; the agent gracefully shuts down all streams and worker threads.

## Integrating custom policies
- Extend `FrameProcessor` to publish VLM outputs into your own policy engine (e.g., tracking sustained entertainment before 17:00 and escalating from notifications to lock/shutdown).
- When escalating, hook into macOS automation (AppleScript, `NSWorkspace`, or MDM tools) after verifying the VLM classifications remain consistent across multiple samples.
- Add watchdog logic to account for macOS 15 Sequoia’s monthly screen-recording permission expiry so long-running agents can re-prompt proactively.

## TODO / next steps
- Cache `litellm` responses or stream partial deltas if classifications prove slow.
- Implement structured output parsing (JSON schema validation) and route to alerting/MDM sinks.
- Add unit tests that inject synthetic frames and mock the VLM client for deterministic automation.
