import os
from dotenv import load_dotenv

load_dotenv()

# FAA TFR data source
FAA_TFR_URL = "https://tfr.faa.gov/tfr3/export/xml"
FAA_TFR_LIST_URL = "https://tfr.faa.gov/tfr_map_ims/html/near_you.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

# Polling intervals
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
WATCH_MODE = os.getenv("WATCH_MODE", "false").lower() in ("1", "true", "yes")
WATCH_INTERVAL_SECONDS = int(os.getenv("WATCH_INTERVAL_SECONDS", "15"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))

# State persistence — Railway persistent volume at /data, local fallback
DB_PATH = os.getenv("DB_PATH", "/data/tfr_monitor.db")

# Telegram — new bot, distinct from Augur Alerts and Augur Intel channels
TFR_TELEGRAM_BOT_TOKEN = os.getenv("TFR_TELEGRAM_BOT_TOKEN", "")
TFR_TELEGRAM_CHAT_ID = os.getenv("TFR_TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() not in ("0", "false", "no")

# Email — reuse Augur Intel's Gmail OAuth2 credentials (same env var names)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "ken.deneau@propher.co")
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() not in ("0", "false", "no")

# Pushover
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_ENABLED = os.getenv("PUSHOVER_ENABLED", "true").lower() not in ("0", "false", "no")

# Severity routing: which channels receive which event types
# Override any key via env vars (comma-separated channel names)
def _parse_channels(env_key: str, default: list[str]) -> list[str]:
    raw = os.getenv(env_key, "")
    if raw.strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    return default

ROUTING = {
    "NEW": _parse_channels("ROUTING_NEW", ["telegram", "email", "pushover"]),
    "CHANGED": _parse_channels("ROUTING_CHANGED", ["telegram", "email", "pushover"]),
    "EXPIRED": _parse_channels("ROUTING_EXPIRED", ["telegram", "email"]),
}


def validate_config() -> None:
    issues: list[str] = []

    if TELEGRAM_ENABLED and not TFR_TELEGRAM_BOT_TOKEN:
        issues.append("TELEGRAM_ENABLED but TFR_TELEGRAM_BOT_TOKEN not set")
    if TELEGRAM_ENABLED and not TFR_TELEGRAM_CHAT_ID:
        issues.append("TFR_TELEGRAM_CHAT_ID not set — send /start to the bot and check @userinfobot for your chat ID")

    if EMAIL_ENABLED and not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
        issues.append("EMAIL_ENABLED but GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN not set")

    if PUSHOVER_ENABLED and not (PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY):
        issues.append("PUSHOVER_ENABLED but PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY not set")

    active = sum([
        TELEGRAM_ENABLED and bool(TFR_TELEGRAM_BOT_TOKEN and TFR_TELEGRAM_CHAT_ID),
        EMAIL_ENABLED and bool(GOOGLE_CLIENT_ID),
        PUSHOVER_ENABLED and bool(PUSHOVER_APP_TOKEN),
    ])

    for issue in issues:
        print(f"CONFIG WARNING: {issue}", flush=True)

    if active == 0:
        raise ValueError(
            "No notification channels are fully configured. "
            "Set TFR_TELEGRAM_CHAT_ID (minimum) to start receiving alerts."
        )

    print(
        f"CONFIG OK — channels active: "
        f"telegram={'yes' if TELEGRAM_ENABLED and TFR_TELEGRAM_CHAT_ID else 'no'} "
        f"email={'yes' if EMAIL_ENABLED and GOOGLE_CLIENT_ID else 'no'} "
        f"pushover={'yes' if PUSHOVER_ENABLED and PUSHOVER_APP_TOKEN else 'no'} "
        f"| interval={POLL_INTERVAL_SECONDS}s "
        f"| watch_mode={'ON (' + str(WATCH_INTERVAL_SECONDS) + 's)' if WATCH_MODE else 'off'}",
        flush=True,
    )
