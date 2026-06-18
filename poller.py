"""
FAA TFR polling loop.

Fetch → parse → diff against stored state → fire notifications.
Never dies on a single bad fetch; uses exponential backoff on repeated failures.
"""
import asyncio
import hashlib
import json
import logging
from typing import Any

import httpx

import config
from database import (
    get_state,
    load_active_tfrs,
    mark_tfr_expired,
    set_state,
    upsert_tfr_entry,
)
from notifiers import TFREvent, notify_all, notify_degraded, notify_recovery

logger = logging.getLogger(__name__)

# Sentinel returned when the endpoint sends 304 Not Modified
_CACHED = object()
# Sentinel returned when the fetch fails (network error, bad body, etc.)
_FAILED = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_hash(tfr: dict) -> str:
    """Stable hash over the fields that matter for change detection."""
    key_fields = {
        "facility": tfr.get("facility", ""),
        "state": tfr.get("state", ""),
        "description": tfr.get("description", ""),
        "creation_date": tfr.get("creation_date", ""),
    }
    blob = json.dumps(key_fields, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _make_link(notam_id: str) -> str:
    """Best-effort detail URL; falls back to the TFR list page."""
    if notam_id and "/" in notam_id:
        # Typical notam_id format: "7/1234" → detail_7_1234.html
        safe = notam_id.replace("/", "_").replace(" ", "_")
        return f"https://tfr.faa.gov/save_pages/detail_{safe}.html"
    return config.FAA_TFR_LIST_URL


def _diff_fields(old: dict, new: dict) -> dict[str, tuple[Any, Any]]:
    """Return {field: (old_val, new_val)} for fields that changed."""
    watch = ("facility", "state", "description", "creation_date")
    return {
        f: (old.get(f), new.get(f))
        for f in watch
        if old.get(f) != new.get(f)
    }


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def _fetch_raw() -> Any:
    """
    Fetch the FAA TFR feed.
    Returns:
      list[dict]  — parsed JSON (may be empty list)
      _CACHED     — 304 Not Modified, no processing needed
      _FAILED     — unrecoverable error this cycle (will be retried next poll)
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://tfr.faa.gov/tfr_map_ims/html/near_you.html",
        "User-Agent": config.USER_AGENT,
    }

    # Conditional request headers — skip if endpoint doesn't honour them
    if not await get_state("conditional_requests_disabled"):
        etag = await get_state("etag")
        last_modified = await get_state("last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(config.FAA_TFR_URL, headers=headers)

        if resp.status_code == 304:
            logger.debug("FAA TFR feed: 304 Not Modified")
            return _CACHED

        if resp.status_code != 200:
            logger.warning(f"FAA TFR feed: HTTP {resp.status_code}")
            return _FAILED

        # The endpoint sometimes returns an HTML app-shell ("Loading...") instead
        # of JSON. Detect this and retry next poll without treating it as a data error.
        body = resp.text.strip()
        if not body.startswith("["):
            logger.warning(
                f"FAA TFR feed returned non-JSON body (first 80 chars): {body[:80]!r} "
                f"— will retry next poll"
            )
            return _FAILED

        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"FAA TFR feed: expected list, got {type(data).__name__}")
            return _FAILED

        # Persist ETag / Last-Modified for conditional requests next cycle.
        # If the endpoint never returns a 304, these values just sit unused.
        new_etag = resp.headers.get("ETag", "")
        new_lm = resp.headers.get("Last-Modified", "")
        if new_etag:
            await set_state("etag", new_etag)
        if new_lm:
            await set_state("last_modified", new_lm)

        vips = [item for item in data if item.get("type") == "VIP"]
        logger.info(f"FAA TFR: {len(data)} total TFRs, {len(vips)} VIP")
        return vips

    except httpx.TimeoutException:
        logger.warning("FAA TFR feed: request timed out")
        return _FAILED
    except Exception as e:
        logger.error(f"FAA TFR fetch error: {e}", exc_info=True)
        return _FAILED


# ---------------------------------------------------------------------------
# Diff and notify
# ---------------------------------------------------------------------------

async def _process(vips: list[dict], notifiers: list) -> None:
    """Diff current VIPs against stored state and dispatch notifications."""
    baseline_seeded = (await get_state("baseline_seeded")) == "true"
    seen = await load_active_tfrs()  # {notam_id: {hash, data}}

    current = {v["notam_id"]: v for v in vips if v.get("notam_id")}
    events: list[TFREvent] = []

    if not baseline_seeded:
        # Cold-start: store current state silently. No alerts fired.
        logger.info(
            f"Cold-start baseline: seeding {len(current)} VIP TFR(s) silently (no alerts). "
            f"Future changes will trigger alerts."
        )
        for tfr in current.values():
            await upsert_tfr_entry(tfr, _compute_hash(tfr))
        await set_state("baseline_seeded", "true")
        return

    # --- NEW and CHANGED ---
    for notam_id, tfr in current.items():
        h = _compute_hash(tfr)
        link = _make_link(notam_id)

        if notam_id not in seen:
            logger.info(f"NEW VIP TFR: {notam_id} ({tfr.get('facility', '?')})")
            events.append(TFREvent(
                event_type="NEW",
                notam_id=notam_id,
                facility=tfr.get("facility", ""),
                state=tfr.get("state", ""),
                description=tfr.get("description", ""),
                creation_date=tfr.get("creation_date", ""),
                link=link,
            ))
        elif h != seen[notam_id]["hash"]:
            changed = _diff_fields(seen[notam_id]["data"], tfr)
            logger.info(f"CHANGED VIP TFR: {notam_id} — fields: {list(changed.keys())}")
            events.append(TFREvent(
                event_type="CHANGED",
                notam_id=notam_id,
                facility=tfr.get("facility", ""),
                state=tfr.get("state", ""),
                description=tfr.get("description", ""),
                creation_date=tfr.get("creation_date", ""),
                link=link,
                changed_fields=changed,
            ))

    # --- EXPIRED ---
    for notam_id, entry in seen.items():
        if notam_id not in current:
            tfr = entry["data"]
            logger.info(f"EXPIRED VIP TFR: {notam_id} ({tfr.get('facility', '?')})")
            events.append(TFREvent(
                event_type="EXPIRED",
                notam_id=notam_id,
                facility=tfr.get("facility", ""),
                state=tfr.get("state", ""),
                description=tfr.get("description", ""),
                creation_date=tfr.get("creation_date", ""),
                link=_make_link(notam_id),
            ))

    # --- Persist updated state ---
    for tfr in current.values():
        await upsert_tfr_entry(tfr, _compute_hash(tfr))
    for event in events:
        if event.event_type == "EXPIRED":
            await mark_tfr_expired(event.notam_id)

    # --- Dispatch notifications ---
    for event in events:
        await notify_all(event, notifiers)

    if not events:
        logger.debug("Poll complete — no VIP TFR changes detected")


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

async def poll_loop(notifiers: list, state) -> None:
    """
    Persistent polling loop. Never exits except on process kill.
    Reads state.poll_interval each cycle so Telegram bot commands take effect immediately.

    Backoff strategy on repeated failures:
      failure 1 → 1× interval
      failure 2 → 2× interval
      failure 3 → 4× interval
      failure 4+ → capped at 5 minutes
    """
    consecutive_failures = 0
    degraded_alert_sent = False

    logger.info(f"Poll loop started — interval={state.poll_interval}s")

    while True:
        interval = state.poll_interval

        try:
            result = await _fetch_raw()

            if result is _FAILED:
                consecutive_failures += 1
                backoff = min(300, interval * (2 ** min(consecutive_failures - 1, 4)))
                logger.warning(
                    f"Fetch failed (#{consecutive_failures} consecutive). "
                    f"Backing off {backoff}s."
                )
                if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES and not degraded_alert_sent:
                    logger.error(
                        f"Reached {consecutive_failures} consecutive failures — sending degraded alert"
                    )
                    await notify_degraded(consecutive_failures, notifiers)
                    degraded_alert_sent = True
                await asyncio.sleep(backoff)
                continue

            # Successful response (304 or new data)
            if consecutive_failures > 0:
                logger.info(f"Feed recovered after {consecutive_failures} failure(s)")
                if degraded_alert_sent:
                    await notify_recovery(notifiers)
                    degraded_alert_sent = False
                consecutive_failures = 0

            if result is not _CACHED:
                await _process(result, notifiers)

        except Exception as e:
            # Catch-all so the loop never dies
            consecutive_failures += 1
            logger.error(f"Unexpected poll loop error: {e}", exc_info=True)
            backoff = min(300, interval * (2 ** min(consecutive_failures - 1, 4)))
            await asyncio.sleep(backoff)
            continue

        await asyncio.sleep(interval)
