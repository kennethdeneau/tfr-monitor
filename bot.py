"""
Telegram command listener (long-polling via getUpdates).
Runs as a concurrent asyncio task alongside poll_loop.

Supported commands (from the authorized chat only):
  interval=N   — set poll interval to N seconds
  /interval N  — same
  /status      — show current interval
  /reset       — restore startup default
  /fetch       — hit the FAA feed right now, show VIPs found (no state change)
  /test        — fire a real notification for the first current VIP through all channels
  /reseed      — wipe stored state so next poll fires NEW alerts for all current VIPs
  /help        — show command list
"""
import asyncio
import html as html_lib
import logging
import re
import xml.etree.ElementTree as ET

import httpx

import config
from database import clear_tfr_state, get_state, set_state
from poller import _make_link

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
        if not resp.is_success:
            logger.warning(f"Bot reply failed: {resp.status_code} {resp.text[:80]}")
    except Exception as e:
        logger.warning(f"Bot reply error: {e}")


async def _fetch_vips() -> tuple[list[dict] | None, str | None]:
    """Fetch current VIP TFRs directly. Returns (vips, error_msg)."""
    headers = {
        "Accept": "text/xml,application/xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tfr.faa.gov/tfr_map_ims/html/near_you.html",
        "User-Agent": config.USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(config.FAA_TFR_URL, headers=headers)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        body = resp.text.strip()
        if not body.startswith("<") or body.lower().startswith("<!doctype"):
            return None, f"Feed returned non-XML (first 80 chars: {body[:80]!r})"
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            return None, f"XML parse error: {e}"
        from poller import _parse_xml_tfrs
        data = _parse_xml_tfrs(root)
        vips = [item for item in data if item.get("type") == "VIP"]
        return vips, None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_HELP = (
    "📡 <b>TFR Monitor commands</b>\n\n"
    "<b>Interval control</b>\n"
    "• <code>interval=N</code> — set poll interval to N seconds (min 5, max 3600)\n"
    "• <code>/reset</code> — restore config default\n"
    "• <code>/status</code> — show current interval\n\n"
    "<b>Testing</b>\n"
    "• <code>/fetch</code> — hit FAA feed now, show VIPs found (no state change)\n"
    "• <code>/test</code> — fire a real alert for the first current VIP through all channels\n"
    "• <code>/reseed</code> — wipe state so next poll fires NEW alerts for all current VIPs\n\n"
    "• <code>/help</code> — this message"
)


async def _cmd_interval(m: re.Match, state: MonitorState) -> None:
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


async def _cmd_status(state: MonitorState) -> None:
    default = state.default_interval
    current = state.poll_interval
    watch = " (WATCH_MODE=true)" if config.WATCH_MODE else ""
    saved = await get_state("poll_interval_override")
    override = " | <i>override active</i>" if saved and saved.isdigit() else ""
    baseline = await get_state("baseline_seeded")
    seeded = "✅ seeded" if baseline == "true" else "⚠️ not yet seeded (first poll pending)"
    await _send(
        f"📡 <b>TFR Monitor status</b>\n\n"
        f"Interval: <b>{current}s</b>{override}\n"
        f"Config default: {default}s{watch}\n"
        f"Baseline: {seeded}"
    )


async def _cmd_reset(state: MonitorState) -> None:
    default = state.default_interval
    state.poll_interval = default
    await set_state("poll_interval_override", "")
    await _send(f"✅ Interval reset to config default: <b>{default}s</b>.")
    logger.info(f"Poll interval reset to {default}s via Telegram")


async def _cmd_fetch() -> None:
    await _send("⏳ Fetching FAA TFR feed…")
    vips, err = await _fetch_vips()
    if err:
        await _send(f"❌ Fetch failed: {html_lib.escape(err)}")
        return
    if not vips:
        await _send("ℹ️ Feed returned <b>0 VIP TFRs</b> right now.")
        return

    lines = [f"✅ Found <b>{len(vips)} VIP TFR(s)</b> in the feed:\n"]
    for i, tfr in enumerate(vips, 1):
        nid = tfr.get("notam_id", "?")
        facility = tfr.get("facility", "?")
        state_str = tfr.get("state", "?")
        desc = tfr.get("description", "")[:120]
        link = _make_link(nid)
        lines.append(
            f"{i}. <b>{nid}</b> — {facility} ({state_str})\n"
            f"   {desc}…\n"
            f"   <a href=\"{link}\">detail</a>"
        )
    await _send("\n".join(lines))


async def _cmd_test(notifiers: list) -> None:
    await _send("⏳ Fetching current VIPs and firing a test notification…")
    vips, err = await _fetch_vips()
    if err:
        await _send(f"❌ Fetch failed: {html_lib.escape(err)}")
        return
    if not vips:
        await _send("ℹ️ No VIP TFRs in the feed right now — nothing to test with.")
        return

    from notifiers import TFREvent, notify_all
    tfr = vips[0]
    nid = tfr.get("notam_id", "TEST-0")
    event = TFREvent(
        event_type="NEW",
        notam_id=f"[TEST] {nid}",
        facility=tfr.get("facility", ""),
        state=tfr.get("state", ""),
        description=f"[TEST NOTIFICATION — not a real alert]\n\n{tfr.get('description', '')}",
        creation_date=tfr.get("creation_date", ""),
        link=_make_link(nid),
    )
    await notify_all(event, notifiers)
    await _send(
        f"✅ Test notification fired for <code>{nid}</code> through all active channels.\n"
        f"Check Pushover and email to confirm end-to-end delivery.\n\n"
        f"(State was <b>not</b> modified — this won't affect real alert tracking.)"
    )
    logger.info(f"Test notification fired for {nid}")


async def _cmd_reseed(state: MonitorState) -> None:
    await _send("⏳ Checking feed before wiping state…")
    vips, err = await _fetch_vips()
    if err:
        await _send(f"⚠️ Could not reach feed ({err}), but wiping state anyway.")
        vips = []

    count = await clear_tfr_state()
    vip_count = len(vips) if vips else "unknown"
    interval = state.poll_interval

    await _send(
        f"🗑 State wiped — {count} stored TFR entries cleared.\n\n"
        f"The feed currently shows <b>{vip_count} VIP TFR(s)</b>.\n"
        f"The next poll (within ~{interval}s) will fire <b>NEW</b> alerts for all of them "
        f"through every active channel."
    )
    logger.info(f"State wiped via Telegram: {count} entries cleared, baseline reset")


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

async def _handle(text: str, state: MonitorState, notifiers: list) -> None:
    text = text.strip()

    # interval=N  |  /interval N  |  interval N
    m = re.match(r"^/?interval[=\s]+(\d+)$", text, re.IGNORECASE)
    if m:
        await _cmd_interval(m, state)
        return

    cmd = text.lower().lstrip("/")

    if cmd == "status":
        await _cmd_status(state)
    elif cmd in ("reset", "default"):
        await _cmd_reset(state)
    elif cmd == "fetch":
        await _cmd_fetch()
    elif cmd == "test":
        await _cmd_test(notifiers)
    elif cmd == "reseed":
        await _cmd_reseed(state)
    elif cmd == "help":
        await _send(_HELP)
    else:
        await _send(_HELP)


# ---------------------------------------------------------------------------
# Long-polling listener
# ---------------------------------------------------------------------------

async def bot_listener(state: MonitorState, notifiers: list) -> None:
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

            if not resp.is_success:
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

                if chat_id != str(config.TFR_TELEGRAM_CHAT_ID):
                    logger.debug(f"Ignoring message from unauthorized chat {chat_id}")
                    continue

                await _handle(text, state, notifiers)

        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            pass  # expected during quiet long-poll periods
        except Exception as e:
            logger.error(f"Bot listener error: {e}", exc_info=True)
            await asyncio.sleep(10)
