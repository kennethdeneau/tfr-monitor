"""
Pluggable notifier interface. Each channel is an independent class.
To add a new channel (ntfy, SMS, etc.), subclass Notifier and register it
in build_notifiers(). No other file needs to change.
"""
import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
PUSHOVER_API = "https://api.pushover.net/1/messages.json"


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------

@dataclass
class TFREvent:
    event_type: str          # "NEW" | "CHANGED" | "EXPIRED"
    notam_id: str
    facility: str
    state: str
    description: str
    creation_date: str
    link: str
    changed_fields: dict | None = None  # {field: (old_val, new_val)} for CHANGED

    @property
    def emoji(self) -> str:
        return {"NEW": "🆕", "CHANGED": "🔄", "EXPIRED": "✅"}.get(self.event_type, "❓")

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.event_type} VIP TFR"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Notifier(ABC):
    name: str

    @abstractmethod
    async def send(self, event: TFREvent) -> bool: ...

    def is_enabled(self) -> bool:
        return True

    async def send_degraded(self, consecutive: int) -> bool:
        return False

    async def send_recovery(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram_text(event: TFREvent) -> str:
    lines = [
        f"<b>{event.label}</b>",
        "",
        f"🏛 <b>ARTCC:</b> {event.facility}",
        f"📍 <b>State:</b> {event.state}",
        f"📝 <b>Description:</b>",
        event.description[:1200] + ("…" if len(event.description) > 1200 else ""),
    ]

    if event.event_type == "CHANGED" and event.changed_fields:
        lines.append("")
        lines.append("🔀 <b>Changes:</b>")
        for field_name, (old, new) in event.changed_fields.items():
            old_str = str(old)[:120] if old else "(none)"
            new_str = str(new)[:120] if new else "(none)"
            lines.append(f"  <i>{field_name}:</i>\n  <s>{old_str}</s>\n  → {new_str}")

    lines += [
        "",
        f"🆔 <b>NOTAM:</b> <code>{event.notam_id}</code>",
        f"📅 <b>Created:</b> {event.creation_date}",
        f"🔗 <a href=\"{event.link}\">FAA TFR detail</a>",
    ]
    return "\n".join(lines)


class TelegramNotifier(Notifier):
    name = "telegram"

    def is_enabled(self) -> bool:
        return config.TELEGRAM_ENABLED and bool(config.TFR_TELEGRAM_BOT_TOKEN and config.TFR_TELEGRAM_CHAT_ID)

    async def _post(self, text: str) -> bool:
        url = f"{TELEGRAM_API}/bot{config.TFR_TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.TFR_TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json=payload)
            if not resp.ok:
                logger.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    async def send(self, event: TFREvent) -> bool:
        return await self._post(_telegram_text(event))

    async def send_degraded(self, consecutive: int) -> bool:
        text = (
            f"⚠️ <b>TFR MONITOR DEGRADED</b>\n\n"
            f"FAA TFR feed has failed <b>{consecutive}</b> consecutive polls.\n"
            f"A silent FAA outage could mean new VIPs are not being reported.\n\n"
            f"Check manually: <a href=\"{config.FAA_TFR_LIST_URL}\">FAA TFR map</a>"
        )
        return await self._post(text)

    async def send_recovery(self) -> bool:
        return await self._post("✅ <b>TFR MONITOR RECOVERED</b> — FAA feed is responding normally.")


# ---------------------------------------------------------------------------
# Email (Gmail API — same pattern as Augur Intel)
# ---------------------------------------------------------------------------

def _email_html(event: TFREvent) -> str:
    badge_colors = {"NEW": "#dc3545", "CHANGED": "#fd7e14", "EXPIRED": "#6c757d"}
    badge_color = badge_colors.get(event.event_type, "#333")

    change_html = ""
    if event.event_type == "CHANGED" and event.changed_fields:
        rows = []
        for fname, (old, new) in event.changed_fields.items():
            rows.append(
                f"<tr><td style='color:#666;padding:4px 8px;'>{fname}</td>"
                f"<td style='padding:4px 8px;'><span style='color:#dc3545;text-decoration:line-through'>{old or '(none)'}</span>"
                f" → <span style='color:#198754'>{new or '(none)'}</span></td></tr>"
            )
        change_html = f"""
        <div style='margin:16px 0;'>
          <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:6px;'>Changes</div>
          <table style='border-collapse:collapse;width:100%;font-size:14px;'>{''.join(rows)}</table>
        </div>"""

    desc = event.description.replace("\n", "<br>")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html>
<body style='font-family:Georgia,serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a1a;'>
  <div style='background:#1a1a2e;color:white;padding:20px;border-radius:8px 8px 0 0;'>
    <span style='background:{badge_color};color:white;padding:4px 14px;border-radius:4px;font-weight:bold;font-family:monospace;letter-spacing:1px;'>
      {event.event_type}
    </span>
    <h2 style='margin:10px 0 0 0;font-size:18px;'>VIP Temporary Flight Restriction</h2>
  </div>
  <div style='background:#f8f9fa;padding:24px;border-radius:0 0 8px 8px;'>
    <div style='margin-bottom:14px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;'>ARTCC / Facility</div>
      <div style='font-size:16px;font-weight:bold;'>{event.facility}</div>
    </div>
    <div style='margin-bottom:14px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;'>State</div>
      <div style='font-size:15px;'>{event.state}</div>
    </div>
    <div style='margin-bottom:14px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:6px;'>Description</div>
      <div style='background:white;border-left:4px solid {badge_color};padding:12px 16px;font-size:14px;line-height:1.6;'>{desc}</div>
    </div>
    {change_html}
    <div style='display:flex;gap:24px;margin-top:16px;'>
      <div>
        <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;'>NOTAM ID</div>
        <div style='font-family:monospace;'>{event.notam_id}</div>
      </div>
      <div>
        <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;'>Created</div>
        <div>{event.creation_date}</div>
      </div>
    </div>
    <div style='margin-top:20px;'>
      <a href='{event.link}' style='color:#0d6efd;'>View on FAA TFR Website →</a>
    </div>
    <div style='margin-top:24px;font-size:12px;color:#999;border-top:1px solid #dee2e6;padding-top:12px;'>
      Augur Intel TFR Monitor · {ts}
    </div>
  </div>
</body>
</html>"""


def _email_plain(event: TFREvent) -> str:
    lines = [
        f"{event.label}",
        "=" * 40,
        f"ARTCC: {event.facility}",
        f"State: {event.state}",
        "",
        "Description:",
        event.description,
    ]
    if event.event_type == "CHANGED" and event.changed_fields:
        lines += ["", "Changes:"]
        for fname, (old, new) in event.changed_fields.items():
            lines.append(f"  {fname}: {old} → {new}")
    lines += [
        "",
        f"NOTAM ID: {event.notam_id}",
        f"Created: {event.creation_date}",
        f"Link: {event.link}",
    ]
    return "\n".join(lines)


class EmailNotifier(Notifier):
    name = "email"

    def is_enabled(self) -> bool:
        return config.EMAIL_ENABLED and bool(
            config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.GOOGLE_REFRESH_TOKEN
        )

    def _get_service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=config.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=config.GOOGLE_CLIENT_ID,
            client_secret=config.GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    async def _send_email(self, subject: str, plain: str, html: str) -> bool:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self._send_sync(subject, plain, html))
            return True
        except Exception as e:
            logger.error(f"Email send error: {e}")
            return False

    def _send_sync(self, subject: str, plain: str, html: str) -> None:
        service = self._get_service()
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_RECIPIENT
        msg["To"] = config.EMAIL_RECIPIENT
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Email sent: {subject}")

    async def send(self, event: TFREvent) -> bool:
        subject = f"[TFR ALERT] {event.label} — {event.facility} ({event.creation_date[:10]})"
        return await self._send_email(subject, _email_plain(event), _email_html(event))

    async def send_degraded(self, consecutive: int) -> bool:
        subject = "⚠️ [TFR MONITOR] Feed degraded — check manually"
        plain = (
            f"The FAA TFR feed has failed {consecutive} consecutive polls.\n"
            f"Check manually: {config.FAA_TFR_LIST_URL}"
        )
        html = f"<p>FAA TFR feed failed <b>{consecutive}</b> consecutive polls.</p><p><a href='{config.FAA_TFR_LIST_URL}'>Check FAA TFR map</a></p>"
        return await self._send_email(subject, plain, html)

    async def send_recovery(self) -> bool:
        subject = "✅ [TFR MONITOR] Feed recovered"
        plain = "FAA TFR feed is responding normally."
        return await self._send_email(subject, plain, f"<p>{plain}</p>")


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

_PUSHOVER_PRIORITY = {
    "NEW": 1,       # high — bypasses quiet hours
    "CHANGED": 0,   # normal
    "EXPIRED": -1,  # quiet
}


class PushoverNotifier(Notifier):
    name = "pushover"

    def is_enabled(self) -> bool:
        return config.PUSHOVER_ENABLED and bool(config.PUSHOVER_APP_TOKEN and config.PUSHOVER_USER_KEY)

    async def _post(self, title: str, message: str, priority: int = 0, url: str = "", url_title: str = "") -> bool:
        payload: dict = {
            "token": config.PUSHOVER_APP_TOKEN,
            "user": config.PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if url:
            payload["url"] = url
            payload["url_title"] = url_title or "FAA TFR"
        # Priority 2 (emergency) requires retry + expire fields
        if priority == 2:
            payload["retry"] = 60
            payload["expire"] = 3600
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(PUSHOVER_API, data=payload)
            if not resp.ok:
                logger.error(f"Pushover error {resp.status_code}: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"Pushover send error: {e}")
            return False

    async def send(self, event: TFREvent) -> bool:
        priority = _PUSHOVER_PRIORITY.get(event.event_type, 0)
        title = f"{event.emoji} {event.event_type} VIP TFR — {event.facility}"
        # Pushover message capped at 1024 chars; keep description + key fields
        desc_limit = 600
        desc = event.description[:desc_limit] + ("…" if len(event.description) > desc_limit else "")
        message = f"{desc}\n\nNOTAM: {event.notam_id} | {event.creation_date}"
        return await self._post(title, message, priority, event.link, "FAA TFR detail")

    async def send_degraded(self, consecutive: int) -> bool:
        return await self._post(
            title="⚠️ TFR Monitor Degraded",
            message=f"FAA TFR feed failed {consecutive} consecutive polls. Check manually.",
            priority=1,
            url=config.FAA_TFR_LIST_URL,
            url_title="FAA TFR map",
        )

    async def send_recovery(self) -> bool:
        return await self._post(
            title="✅ TFR Monitor Recovered",
            message="FAA TFR feed is responding normally.",
            priority=0,
        )


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def build_notifiers() -> list[Notifier]:
    all_notifiers: list[Notifier] = [TelegramNotifier(), EmailNotifier(), PushoverNotifier()]
    enabled = [n for n in all_notifiers if n.is_enabled()]
    logger.info(f"Active notifiers: {[n.name for n in enabled]}")
    return enabled


async def notify_all(event: TFREvent, notifiers: list[Notifier]) -> None:
    channels = config.ROUTING.get(event.event_type, [])
    for notifier in notifiers:
        if notifier.name not in channels:
            logger.debug(f"Skipping {notifier.name} for {event.event_type} (routing config)")
            continue
        try:
            ok = await notifier.send(event)
            logger.info(f"[{notifier.name}] {event.event_type} {event.notam_id}: {'sent' if ok else 'FAILED'}")
        except Exception as e:
            logger.error(f"[{notifier.name}] unhandled error for {event.notam_id}: {e}", exc_info=True)


async def notify_degraded(consecutive: int, notifiers: list[Notifier]) -> None:
    for notifier in notifiers:
        if notifier.name in ("telegram", "pushover"):
            try:
                await notifier.send_degraded(consecutive)
            except Exception as e:
                logger.error(f"[{notifier.name}] degraded alert error: {e}")


async def notify_recovery(notifiers: list[Notifier]) -> None:
    for notifier in notifiers:
        if notifier.name in ("telegram", "pushover"):
            try:
                await notifier.send_recovery()
            except Exception as e:
                logger.error(f"[{notifier.name}] recovery alert error: {e}")
