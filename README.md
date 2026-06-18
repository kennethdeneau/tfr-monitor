# Augur Intel TFR Monitor

Always-on worker that polls the FAA Temporary Flight Restriction feed and alerts on **VIP (presidential-level) TFRs** via Telegram, email, and Pushover. Runs as a Railway persistent worker service.

---

## Environment Variables

### Polling

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | Normal polling interval in seconds |
| `WATCH_MODE` | `false` | Set `true` to use the tighter watch interval |
| `WATCH_INTERVAL_SECONDS` | `15` | Polling interval when `WATCH_MODE=true` |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Consecutive fetch failures before a degraded alert fires |

**Watch mode** is meant to be flipped on via Railway environment variable when you expect a same-day VIP TFR. Toggle it in Railway Settings → Variables, then trigger a redeploy (or wait for the next deploy). Toggle it back off the same way once the event passes.

### State Persistence

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/tfr_monitor.db` | Path to the SQLite state file |

The worker uses **SQLite on a Railway persistent volume**. In Railway:
1. Go to your service → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. That's it. The worker creates the database on first boot.

On restart or redeploy, the last-seen TFR state is read back from disk. The cold-start guard means a fresh boot (or after a state wipe) seeds the baseline silently — no alert storm.

### Telegram

| Variable | Default | Description |
|---|---|---|
| `TFR_TELEGRAM_BOT_TOKEN` | *(required)* | Token for the dedicated TFR Telegram bot |
| `TFR_TELEGRAM_CHAT_ID` | *(required)* | Chat ID to send alerts to |
| `TELEGRAM_ENABLED` | `true` | Set `false` to disable Telegram entirely |

**How to get `TFR_TELEGRAM_CHAT_ID`:**
1. Open Telegram and search for the bot (`@` + bot username)
2. Send `/start`
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser — look for `"chat": {"id": ...}`
4. Or message [@userinfobot](https://t.me/userinfobot) which replies with your personal chat ID

This bot is deliberately **separate** from the Augur Alerts and Augur Intel bots so TFR alerts arrive on a distinct channel.

### Email

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLIENT_ID` | *(required)* | Same credential as Augur Intel |
| `GOOGLE_CLIENT_SECRET` | *(required)* | Same credential as Augur Intel |
| `GOOGLE_REFRESH_TOKEN` | *(required)* | Same credential as Augur Intel |
| `EMAIL_RECIPIENT` | `ken.deneau@propher.co` | Recipient address |
| `EMAIL_ENABLED` | `true` | Set `false` to disable email |

Reuses the Gmail OAuth2 credentials from Augur Intel. No additional Google Cloud setup needed — copy the three `GOOGLE_*` values directly from the Augur Intel Railway service.

### Pushover

| Variable | Default | Description |
|---|---|---|
| `PUSHOVER_APP_TOKEN` | *(required)* | Pushover application token |
| `PUSHOVER_USER_KEY` | *(required)* | Pushover user/group key |
| `PUSHOVER_ENABLED` | `true` | Set `false` to disable Pushover |

Priority mapping (not overridable without code change):
- `NEW` → priority 1 (high — bypasses quiet hours)
- `CHANGED` → priority 0 (normal)
- `EXPIRED` → skipped by default (email + Telegram only)

### Severity Routing

Override which channels receive which event types:

| Variable | Default |
|---|---|
| `ROUTING_NEW` | `telegram,email,pushover` |
| `ROUTING_CHANGED` | `telegram,email,pushover` |
| `ROUTING_EXPIRED` | `telegram,email` |

Set to a comma-separated list of channel names. Example — to skip email on CHANGED events: `ROUTING_CHANGED=telegram,pushover`

---

## How State Persistence Works

```
/data/tfr_monitor.db   ← Railway persistent volume (survives redeploys)
  ├── tfr_entries      ← one row per VIP TFR ever seen
  │     notam_id (PK), facility, state, description, creation_date,
  │     data_hash, full_data (JSON), first_seen_at, last_seen_at, is_active
  └── app_state        ← key/value store
        baseline_seeded = "true" after first successful poll
        etag            = last ETag from FAA for conditional requests
        last_modified   = last Last-Modified header from FAA
```

**Cold-start guard:** On first run with no prior state (or after a volume wipe), the worker seeds all currently-active VIP TFRs as the baseline and fires **no alerts**. Only changes after that baseline are alerted. This prevents alert storms on deploy/redeploy.

**Conditional requests:** The worker sends `If-None-Match` / `If-Modified-Since` headers if the FAA feed returned them on the previous poll. If the endpoint returns `304 Not Modified`, processing is skipped for that cycle. If the endpoint doesn't honour conditional requests, the code falls back gracefully to a full fetch + diff every cycle.

---

## Diffing Logic

Each poll:
1. Fetch all current VIP TFRs from the FAA feed
2. Load the previously-stored active TFRs from SQLite
3. Compute `NEW` (notam_id not in state), `CHANGED` (notam_id seen but hash differs), `EXPIRED` (previously seen, now absent)
4. For `CHANGED` events, show field-level diff (facility, state, description, creation_date)
5. Persist updated state to SQLite
6. Fan out notifications to each configured channel

---

## Railway Deployment

1. Create a new GitHub repo and push this directory to it
2. In Railway: **New Project → Deploy from GitHub repo** → select the new repo
3. Add a **Volume** mounted at `/data`
4. Set all required environment variables (see above)
5. Railway will run `python main.py` and keep it alive

The worker logs to stdout — visible in Railway's log pane.

---

## Adding a New Notification Channel

1. Subclass `Notifier` in `notifiers.py`:
   ```python
   class NtfyNotifier(Notifier):
       name = "ntfy"
       def is_enabled(self) -> bool: ...
       async def send(self, event: TFREvent) -> bool: ...
   ```
2. Add it to `build_notifiers()`:
   ```python
   all_notifiers = [TelegramNotifier(), EmailNotifier(), PushoverNotifier(), NtfyNotifier()]
   ```
3. Add it to any routing defaults in `config.py` if you want it on by default

Nothing else changes.

---

## What Was Reused vs Built New

| Component | Source | Notes |
|---|---|---|
| Gmail OAuth2 send pattern | Augur Intel `intelligence/briefing.py` | Same 4 env vars, same `google-api-python-client` call |
| `httpx.AsyncClient(timeout=30)` pattern | Augur Intel `portfolio/kalshi_client.py` | Identical HTTP client usage |
| `logging.basicConfig` format | Augur Intel `main.py` | Same `%(asctime)s %(levelname)s [%(name)s]` format |
| `os.getenv()` + `validate_config()` | Augur Intel `config.py` | Same pattern, adapted for TFR env vars |
| `DB_PATH` at `/data/...` | Augur Intel `config.py` | Same Railway volume convention |
| `aiosqlite` + `app_state` table | Augur Intel `database.py` | Same key/value state store pattern |
| `asyncio.sleep` poll loop | Augur Intel `utils/scheduler.py` | Simplified (no windowed schedule needed) |
| Telegram HTTPS POST | Augur Alerts `server/telegram-bot.ts` | Reimplemented in Python; same API |
| Pluggable notifier interface | New | Abstract base class; add channels without refactoring |
| FAA TFR fetch + HTML-shell detection | New | `Accept: application/json` + body prefix check |
| Diff engine (NEW/CHANGED/EXPIRED) | New | Hash-based, persisted in SQLite |
| Pushover notifier | New | Pure HTTPS POST to `api.pushover.net` |
| Exponential backoff + degraded alert | New | Caps at 5 minutes; alerts once on entry + once on recovery |
| Cold-start guard | New | `baseline_seeded` flag in `app_state` |
