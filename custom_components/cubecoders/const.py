"""Constants for the AMP integration."""

DOMAIN = "cubecoders"

# Optional base32 TOTP secret for AMP users that require 2FA.
CONF_TOTP_SECRET = "totp_secret"

# Polling. The interval is user-configurable via the integration's options;
# after a start/stop/restart/update, the integration briefly polls fast so
# the new state shows up without waiting for the next regular poll.
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 600
FAST_POLL_INTERVAL = 1  # seconds; lightweight polls (one API call each)
SETTLE_SECONDS = 10  # settled this long after a change -> burst ends
NO_CHANGE_TIMEOUT = 30  # no change seen this long -> stop once settled
MAX_FAST_POLL_DURATION = 900  # hard cap (long game updates)

# AMP application states that mean an action is still in progress.
IN_PROGRESS_APP_STATES = frozenset(
    {
        "pre_start",
        "configuring",
        "starting",
        "restarting",
        "stopping",
        "preparing_for_sleep",
        "waiting",
        "installing",
        "updating",
    }
)

# URL base for assets bundled with the integration (registered in __init__).
STATIC_URL_BASE = "/cubecoders_static"

# Simplified per-instance status (the Status sensor), derived from whether
# the instance is running and its AMP application state.
STATUS_OFFLINE = "offline"  # instance off
STATUS_IDLE = "idle"  # instance on, application stopped
STATUS_RUNNING = "running"  # instance on, application ready
STATUS_UNKNOWN = "unknown"
APP_STATE_TO_STATUS = {
    "ready": STATUS_RUNNING,
    "stopped": STATUS_IDLE,
    "pre_start": "starting",
    "configuring": "starting",
    "starting": "starting",
    "waiting": "starting",
    "restarting": "restarting",
    "stopping": "stopping",
    "preparing_for_sleep": "stopping",
    "updating": "updating",
    "installing": "installing",
    "sleeping": "sleeping",
    "failed": "failed",
}
STATUS_OPTIONS = [
    STATUS_OFFLINE,
    STATUS_IDLE,
    STATUS_RUNNING,
    "starting",
    "stopping",
    "restarting",
    "updating",
    "installing",
    "sleeping",
    "failed",
    STATUS_UNKNOWN,
]


def instance_status(*, running: bool, app_state: str | None) -> str:
    """Map (instance running, AMP app state) to the simplified status."""
    if not running:
        return STATUS_OFFLINE
    return APP_STATE_TO_STATUS.get(app_state or "", STATUS_UNKNOWN)
