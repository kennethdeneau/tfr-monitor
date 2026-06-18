"""
Telegram command listener (long-polling via getUpdates).
Runs as a concurrent asyncio task alongside poll_loop.

Supported commands (from the authorized chat only):
  interval=N   — set poll interval to N seconds
  /interval N  — same
  /status      — show current interval and run state
  /reset       — restore startup default
  /help        — show command list
"""
import asyncio
import logging
import re

import httpx

import config
from database import get_state, set_state

logger = logging.getLogger(__name__)


class MonitorState:
    """Shared mutable state passed to both poll_loop and bot_listener."""

    def __init__(self, poll_interval: int) -> None:
        self.poll_interval = poll_interval

    @property
    def default_interval(self) -> int:
        return config.WATCH_INTERVAL_SECONDS if config.WATCH_MODE else config.POLL_INTERVAL_SECONDS


async def load_state() -> MonitorState:
    """Restore persisted interval override from DB, fall back to config."""
    saved = await get_state("poll_interval_override")
    if saved and saved.isdigit():
        interval = int(saved)
        logger.info(f"Restoring saved poll interval: {interval}s")
    else:
        interval = config.WATCH_INTERVAL_SECONDS if config.WATCH_MODE else config.POLL_INTERVAL_SECONDS
    return MonitorState(poll_interval=interval)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def _send(text: str) -> None:
    if not (config.TFR_TELEGRAM_BOT_TOKEN and config.TFR_TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{config.TFR_TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TFR_TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
        if not resp.ok:
            logger.warning(f"Bot reply failed: {resp.status_code} {resp.text[:80]}")
    except Exception as e:
        logger.warning(f"Bot reply error: {e}")


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

_HELP = (
    "📡 <b>TFR Monitor commands</b>\n\n"
    "• <code>interval=N</code> — set poll interval to N seconds (min 5, max 3600)\n"
    "• <code>/status</code> — show current interval\n"
    "• <code>/reset</code> — restore startup default\n"
    "• <code>/help</code> — this message"
)


async def _handle(text: str, state: MonitorState) -> None:
    text = text.strip()

    # interval=N  |  /interval N  |  interval N
    m = re.match(r"^/?interval[=\s]+(\d+)$", text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n < 5:
            await _send("⚠️ Minimum interval is <b>5 seconds</b>.")
            return
        if n > 3600:
            await _send("⚠️ Maximum interval is <b>3600 seconds</b> (1 hour).")
            return
        state.poll_interval = n
        await set_state("poll_interval_override", str(n))
        await _send(f"✅ Poll interval set to <b>{n}s</b>.")
        logger.info(f"Poll interval updated to {n}s via Telegram")
        return

    cmd = text.lower().lstrip("/")

    if cmd == "status":
        default = state.default_interval
        current = state.poll_interval
        watch = " (WATCH_MODE on)" if config.WATCH_MODE else ""
        saved = await get_state("poll_interval_override")
        override = f" | override active" if saved and saved.isdigit() else ""
        await _send(
            f"📡 <b>TFR Monitor status</b>\n\n"
            f"Current interval: <b>{current}s</b>{override}\n"
            f"Config default: {default}s{watch}"
        )
        return

    if cmd in ("reset", "default"):
        default = state.default_interval
        state.poll_interval = default
        await set_state("poll_interval_override", "")
        await _send(f"✅ Interval reset to config default: <b>{default}s</b>.")
        logger.info(f"Poll interval reset to default {default}s via Telegram")
        return

    if cmd == "help":
        await _send(_HELP)
        return

    # Unknown input — show help
    await _send(_HELP)


# ---------------------------------------------------------------------------
# Long-polling listener
# ---------------------------------------------------------------------------

async def bot_listener(state: MonitorState) -> None:
    """
    Polls getUpdates with a 30-second long-poll timeout.
    Ignores messages from any chat other than TFR_TELEGRAM_CHAT_ID.
    Never exits; errors are logged and retried.
    """
    if not (config.TFR_TELEGRAM_BOT_TOKEN and config.TFR_TELEGRAM_CHAT_ID):
        logger.warning("Bot listener disabled — TFR_TELEGRAM_BOT_TOKEN or TFR_TELEGRAM_CHAT_ID not set")
        return

    url = f"https://api.telegram.org/bot{config.TFR_TELEGRAM_BOT_TOKEN}/getUpdates"
    offset = 0
    logger.info("Telegram bot listener started")

    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.get(
                    url,
                    params={
                        "timeout": 30,
                        "offset": offset,
                        "allowed_updates": ["message"],
                    },
                )

            if not resp.ok:
                logger.warning(f"getUpdates HTTP {resp.status_code} — retrying in 10s")
                await asyncio.sleep(10)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if not text:
                    continue

                # Security: ignore messages not from the authorized chat
                if chat_id != str(config.TFR_TELEGRAM_CHAT_ID):
                    logger.debug(f"Ignoring message from unauthorized chat {chat_id}")
                    continue

                await _handle(text, state)

        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            # Long-poll timeout — expected when there are no updates; just loop
            pass
        except Exception as e:
            logger.error(f"Bot listener error: {e}", exc_info=True)
            await asyncio.sleep(10)
