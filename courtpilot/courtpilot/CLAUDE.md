# CourtPilot — Build Instructions for Claude Code

## What This Project Is

CourtPilot is a SaaS platform for Indian litigation lawyers to track court cases and receive advance hearing notifications. It scrapes India's eCourts portal and delivers alerts via Telegram (free), Email (free), and WhatsApp (premium add-on).

**Target users**: Litigation lawyers in India who currently rely on the clunky eCourts app/website and get same-day notifications, leading to missed hearings and unpreparedness.

**Core value prop**: Advance notifications — weekly digest + 3-day + 2-day + 1-day reminders before each hearing, plus instant alerts for new court orders.

## Architecture

```
FastAPI Backend ←→ PostgreSQL (users, cases, notifications)
     ↕
Celery Workers → Redis (task broker + rate limiting + dedup cache)
     ↕
eCourts Scraper (bharat-courts SDK) → polls every 6-12 hours
     ↕
Notification Pipeline → Telegram Bot (free) / Email (free) / WhatsApp API (premium)
```

## What's Already Built

These files are complete and working. Do NOT rewrite them from scratch — extend and integrate them:

### `requirements.txt`
All dependencies pinned. Key ones:
- `bharat-courts[ocr]==0.5.0` — eCourts scraping SDK with CAPTCHA OCR. 0.5.0 is the first release with `case_status_by_cnr()`; it needs `httpx>=0.28`
- `fastapi==0.115.0` + `uvicorn` — API server
- `sqlalchemy==2.0.35` + `alembic` + `psycopg2-binary` — ORM + migrations
- `celery==5.4.0` + `redis` — async task queue
- `python-telegram-bot==22.5` — Telegram bot (v21 pins `httpx~=0.27`, which conflicts with bharat-courts)
- `httpx==0.28.1` — async HTTP client (scraper fallback, Telegram/WhatsApp senders)
- `asyncpg` — async Postgres driver for the app; Alembic stays on `psycopg2`
- `aiosmtplib` + `jinja2` — email notifications
- `python-jose` + `passlib` — JWT auth
- `tenacity` — retry logic
- `apscheduler` — notification scheduling

### `config/settings.py`
Pydantic Settings class loading from `.env`. All config keys defined with sensible defaults.

### `models/database.py`
Full SQLAlchemy ORM — **this is the source of truth for the data model**:
- `User` — auth (phone-based OTP + optional email/password), profile (name, bar registration), notification channel IDs (telegram_chat_id, whatsapp_number), subscription (plan tier, expiry, max_cases), preferences (notification_time, timezone, digest_day)
- `CourtCase` — master case record keyed by CNR number. Shared across users. Contains all case fields (parties, advocates, court info, status, hearing dates, orders, acts/sections). Has `data_hash` for diff detection and `last_polled_at` for scheduling polls.
- `TrackedCase` — join table: which user tracks which case. Per-user metadata (label, notes, client_name, priority) and per-case notification preferences (notify_telegram, notify_email, notify_whatsapp).
- `CaseSnapshot` — point-in-time snapshots stored when changes detected. Contains `changes` JSON showing what changed.
- `NotificationLog` — delivery tracking with cost (for WhatsApp cost accounting).
- Enums: `PlanTier` (free/starter/pro/firm), `CourtType`, `NotificationChannel`, `NotificationType`, `CaseStatus`
- `PLAN_LIMITS` dict: Free=5 cases/₹0, Starter=50/₹299, Pro=100/₹499, Firm=500/₹1499
- `WHATSAPP_ADDON_PRICE` = ₹199/month

### `scraper/ecourts.py`
Async scraper with two classes:
- `ECourtsScraper` — fetches case data. Primary method: `fetch_case_by_cnr(cnr)`: routes High Court CNRs (detected by `bharat_courts.infer_court_from_cnr`) to `HCServicesClient.case_status_by_cnr`, everything else to `DistrictCourtClient.case_status_by_cnr`, and flattens the SDK's `CaseDetail` via `_case_detail_to_raw()`. Raises `CaseNotFoundError` (not retried) for bad/unknown CNRs. `fetch_cases_by_advocate(name, state, bar_code=)` uses High Court advocate search (district portals have none); `state` accepts an HC code, court name or state name (`resolve_high_court`). `fetch_cause_list()` still targets the old SDK API and is unused. The mobile-API fallback endpoint is unverified and off unless `ECOURTS_MOBILE_API_FALLBACK=true`. Rate-limited via asyncio.Semaphore. Retries with tenacity. Normalizes data from varying eCourts field names into a standard schema. Computes SHA-256 hash of key fields for change detection.
- `CaseDiffDetector` — compares old vs new case data, returns `{field: {old, new}}` changes dict. `should_notify()` maps changes to notification types (date_changed, new_order, status_changed, case_update).

## What Needs to Be Built

Build these in the order listed. Each section describes what to create and the key design decisions.

### 1. Project Scaffolding & Init Files

Create `__init__.py` in every package directory: `app/`, `bot/`, `config/`, `models/`, `notifications/`, `scraper/`, `workers/`.

Create `app/main.py` — FastAPI app entry point:
- Mount routers for auth, cases, users, webhooks
- Add CORS middleware (allow all origins for now)
- Health check endpoint at `/health`
- Lifespan handler to init DB connection pool

Create `models/session.py`:
- Async SQLAlchemy session factory using `DATABASE_URL` from settings
- `get_db()` dependency for FastAPI

Create Alembic setup:
```bash
alembic init alembic
```
- Point `alembic/env.py` to `models.database.Base.metadata`
- Generate initial migration from the existing models

### 2. Auth System (`app/auth/`)

Phone-based OTP authentication (primary) + optional email/password:

- `app/auth/router.py` — FastAPI router:
  - `POST /auth/send-otp` — send 6-digit OTP to phone via SMS (use a simple in-memory or Redis store with 5-min TTL for MVP; integrate SMS gateway like MSG91 later)
  - `POST /auth/verify-otp` — verify OTP, create user if first time, return JWT
  - `POST /auth/refresh` — refresh JWT token
- `app/auth/jwt.py` — JWT token creation/verification using python-jose, SECRET_KEY from settings
- `app/auth/dependencies.py` — `get_current_user()` FastAPI dependency that extracts user from JWT

For MVP: skip actual SMS sending. Accept any OTP in debug mode (`DEBUG=True`). Store OTPs in Redis with TTL.

### 3. Case Management API (`app/cases/`)

- `app/cases/router.py`:
  - `POST /cases/track` — add a case by CNR number. Fetches from eCourts if not in DB. Checks plan limits before adding.
  - `GET /cases/` — list user's tracked cases with filters (status, next_hearing_date range, search)
  - `GET /cases/{case_id}` — full case details with all fields, orders, snapshots
  - `DELETE /cases/{case_id}/untrack` — stop tracking (doesn't delete CourtCase, just TrackedCase)
  - `PUT /cases/{case_id}` — update user-specific metadata (label, notes, client_name, priority)
  - `POST /cases/search-advocate` — bulk search by advocate name, returns matching cases for easy onboarding
  - `GET /cases/upcoming` — cases with hearings in next 7 days, sorted by date

- `app/cases/schemas.py` — Pydantic request/response models
- `app/cases/service.py` — business logic layer (plan limit checks, calling scraper, etc.)

### 4. Telegram Bot (`bot/`)

This is the PRIMARY notification delivery channel (free for all users).

- `bot/telegram_bot.py` — main bot using python-telegram-bot v22 (async):
  - `/start` — welcome message, ask for phone number to link account
  - `/link <phone>` — link Telegram chat to CourtPilot account (stores `telegram_chat_id` on User)
  - `/cases` — list tracked cases with inline keyboard
  - `/case <cnr>` — show specific case details
  - `/upcoming` — hearings in next 7 days
  - `/help` — command reference
  - Webhook mode for production (`TELEGRAM_WEBHOOK_URL` in settings), polling mode for dev

- `bot/message_templates.py` — formatted message templates:
  - `format_case_summary(case)` — single case card with all key info
  - `format_hearing_reminder(case, days_until)` — "Your hearing in X vs Y is in N days"
  - `format_weekly_digest(cases)` — grouped by date, all upcoming hearings
  - `format_new_order_alert(case, order)` — new order uploaded notification with link
  - Use Telegram MarkdownV2 formatting. Include eCourts web links.

- `app/webhooks/telegram.py` — FastAPI endpoint to receive Telegram webhook updates and pass to bot

### 5. Celery Workers (`workers/`)

- `workers/celery_app.py` — Celery app configuration:
  - Broker: Redis (`REDIS_URL`)
  - Result backend: Redis
  - Task serializer: JSON
  - Timezone: Asia/Kolkata
  - Beat schedule for periodic tasks

- `workers/tasks/poll_cases.py` — case polling worker:
  - `poll_all_cases()` — beat task runs every `ECOURTS_POLL_INTERVAL_HOURS` (default 6h):
    1. Query all distinct `CourtCase` records that have at least one `TrackedCase`
    2. Prioritize: cases with hearing in next 7 days polled first, others by `last_polled_at` ascending
    3. Skip cases polled within last 4 hours
    4. For each case: call `ECourtsScraper.fetch_case_by_cnr()`, compare `data_hash`, if changed → run `CaseDiffDetector`, save `CaseSnapshot`, update `CourtCase`, trigger notifications
    5. Rate limit: respect `ECOURTS_RATE_LIMIT_PER_MINUTE` (default 30)
    6. Error handling: increment `poll_error_count`, back off after 3 consecutive failures

  **Critical deduplication logic**: If 50 lawyers track the same CNR, poll it ONCE. The `CourtCase` table is the shared record. This is already handled by the data model — just query distinct case IDs from `tracked_cases` join.

- `workers/tasks/send_notifications.py` — notification dispatch:
  - `send_hearing_reminders()` — beat task runs daily at 8 AM IST:
    1. Find cases with `next_hearing_date` in [today+1, today+2, today+3] days
    2. For each case, find all users tracking it via `TrackedCase`
    3. For each user, check which channels are enabled (notify_telegram, notify_email, notify_whatsapp)
    4. Dispatch to appropriate channel, log in `NotificationLog`

  - `send_weekly_digest()` — beat task runs every Monday at `WEEKLY_DIGEST_TIME` (8 AM):
    1. For each active user, get all their tracked cases with upcoming hearings
    2. Group by date, format digest, send via enabled channels

  - `send_case_update(case_id, changes)` — triggered on-demand when polling detects changes:
    1. Find all users tracking this case
    2. Format change notification (new hearing date, new order, status change)
    3. Send via enabled channels

### 6. Notification Channels (`notifications/`)

- `notifications/telegram.py`:
  - `send_telegram_message(chat_id, text, parse_mode="MarkdownV2")` — send via Bot API
  - `send_telegram_document(chat_id, file_url, caption)` — for sending order PDFs
  - Handle rate limits (Telegram allows 30 msgs/sec to different chats)
  - Cost: FREE (Telegram Bot API has no per-message charges)

- `notifications/email.py`:
  - `send_email(to, subject, html_body)` — async SMTP using aiosmtplib
  - HTML templates in `notifications/templates/` using Jinja2:
    - `hearing_reminder.html`
    - `weekly_digest.html`
    - `new_order.html`
    - `case_update.html`
  - Cost: FREE (SMTP, no per-message charges beyond server costs)

- `notifications/whatsapp.py`:
  - `send_whatsapp_message(phone, template_name, parameters)` — Meta Cloud API
  - Pre-approved message templates (required by WhatsApp Business API):
    - `hearing_reminder` — "Reminder: {{case_title}} hearing on {{date}} at {{court}}"
    - `weekly_digest` — "Weekly Case Summary: {{count}} hearings this week..."
    - `new_order` — "New order uploaded in {{case_title}}. View: {{link}}"
  - Track cost per message in `NotificationLog.cost_inr` (~₹0.50-0.80 per message)
  - Only send if user has `whatsapp_addon=True` on their plan
  - Cost: ~₹0.50-0.80 per message. This is why WhatsApp is a PAID add-on at ₹199/month.

- `notifications/dispatcher.py`:
  - `dispatch_notification(user, case, notification_type, content)` — routes to correct channel based on user preferences and plan
  - Checks plan eligibility (WhatsApp requires addon)
  - Logs all sends to `NotificationLog`

### 7. User Management API (`app/users/`)

- `GET /users/me` — current user profile
- `PUT /users/me` — update profile (name, bar_registration, notification preferences)
- `PUT /users/me/notifications` — update notification preferences (time, channels, digest day)
- `GET /users/me/plan` — current plan details, usage (cases tracked vs limit)
- `POST /users/me/plan/upgrade` — upgrade plan (integrate with payment gateway later; for MVP, just update the tier)

### 8. Docker Setup

- `Dockerfile` — Python 3.12 slim, install requirements, copy app
- `docker-compose.yml`:
  ```yaml
  services:
    api:        # FastAPI app on port 8000
    worker:     # Celery worker
    beat:       # Celery beat scheduler
    bot:        # Telegram bot (webhook receiver integrated in API, or separate polling process)
    postgres:   # PostgreSQL 16
    redis:      # Redis 7
  ```
- `.env.example` — template with all required env vars
- `alembic.ini` — Alembic config pointing to DATABASE_URL

### 9. Single-Page Case View

Create a simple Jinja2 HTML template served by FastAPI:
- `GET /case/{cnr_number}/view` — public page showing:
  - Case title (petitioner vs respondent)
  - Current status, stage, judge
  - Next hearing date (highlighted)
  - Court details with link to eCourts page
  - List of orders with download links
  - Timeline of hearing dates
- Mobile-first responsive design (lawyers access on phones)
- This is the link shared in notifications — "View full details: courtpilot.in/case/DLWE..."

## Key Design Decisions (Already Made)

1. **Telegram is the free channel** — Bot API is completely free, no per-message charges
2. **WhatsApp is premium** at ₹199/month add-on — Meta Cloud API charges ₹0.50-0.80 per message
3. **Email is free** — SMTP costs are negligible
4. **Phone-based auth with OTP** — lawyers in India primarily use phone numbers
5. **CNR number is the case identifier** — unique 16-char code assigned to every case in India
6. **Shared case records** — if 50 lawyers track the same case, poll it once (CourtCase is shared, TrackedCase is per-user)
7. **No web frontend for MVP** — Telegram bot IS the interface. Case page is a simple server-rendered HTML.
8. **eCourts mobile API** — no CAPTCHA, the bharat-courts SDK handles scraping with OCR fallback
9. **IST timezone** — all lawyers are in India, all times in Asia/Kolkata

## Notification Schedule

| Type | When | Content |
|------|------|---------|
| Weekly digest | Monday 8:00 AM IST | All hearings for the coming week |
| 3-day reminder | 3 days before hearing, 8:00 AM | "Hearing in 3 days" |
| 2-day reminder | 2 days before hearing, 8:00 AM | "Hearing in 2 days" |
| 1-day reminder | 1 day before hearing, 7:00 PM | "Hearing tomorrow" (evening reminder) |
| New order | Immediately on detection | "New order uploaded" with link |
| Case update | Immediately on detection | Status/judge/date change alert |

## Pricing

| Plan | Max Cases | Monthly Price | Channels |
|------|-----------|--------------|----------|
| Free | 5 | ₹0 | Telegram + Email |
| Starter | 50 | ₹299 | Telegram + Email |
| Pro | 100 | ₹499 | Telegram + Email |
| Firm | 500 | ₹1,499 | Telegram + Email |
| WhatsApp Add-on | — | +₹199/month | Adds WhatsApp to any plan |

## Environment Variables Needed

```
# Database
DATABASE_URL=postgresql://courtpilot:password@localhost:5432/courtpilot

# Redis
REDIS_URL=redis://localhost:6379/0

# App
SECRET_KEY=generate-a-secure-random-key
DEBUG=false

# Telegram Bot (get from @BotFather on Telegram)
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/webhook/telegram

# WhatsApp Business API (Meta Cloud API)
WHATSAPP_PHONE_NUMBER_ID=your-phone-number-id
WHATSAPP_ACCESS_TOKEN=your-access-token

# Email SMTP
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
EMAIL_FROM=CourtPilot <notifications@courtpilot.in>
```

## Build Order

Follow this exact sequence:
1. Scaffolding — `__init__.py` files, `app/main.py`, DB session, Alembic init
2. Auth — OTP flow, JWT, user creation
3. Case tracking API — CRUD endpoints, scraper integration
4. Telegram bot — link accounts, send messages, bot commands
5. Celery workers — polling task, diff detection, notification dispatch
6. Notification pipeline — Telegram sender, email sender, WhatsApp sender
7. Scheduled notifications — hearing reminders, weekly digest, beat schedule
8. Case view page — single-page HTML with Jinja2
9. Docker setup — Dockerfile, docker-compose, .env.example
10. Testing — test each component, end-to-end flow

## Running Locally for Development

```bash
# 1. Start Postgres and Redis
docker run -d --name courtpilot-db -p 5432:5432 -e POSTGRES_DB=courtpilot -e POSTGRES_USER=courtpilot -e POSTGRES_PASSWORD=courtpilot postgres:16
docker run -d --name courtpilot-redis -p 6379:6379 redis:7

# 2. Install deps
pip install -r requirements.txt

# 3. Setup env
cp .env.example .env  # edit with your values

# 4. Run migrations
alembic upgrade head

# 5. Start API
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 6. Start Celery worker (separate terminal)
celery -A workers.celery_app worker -l info

# 7. Start Celery beat (separate terminal)
celery -A workers.celery_app beat -l info
```

## Testing a Case Fetch

```python
import asyncio
from scraper.ecourts import ECourtsScraper

async def test():
    scraper = ECourtsScraper()
    # Use any valid CNR number — this is the unique case identifier
    case = await scraper.fetch_case_by_cnr("GJRJ060015282018")
    print(case)
    await scraper.close()

asyncio.run(test())
```

## Important Notes

- CNR lookups go through the eCourts web portals, which require a CAPTCHA; the `bharat-courts` SDK (MIT license) solves it with OCR (ddddocr) and retries up to 5 times. A lookup can take tens of seconds, which is why the bot/webhook process updates concurrently
- A no-CAPTCHA mobile API was the original plan, but the endpoint in `_fetch_via_mobile_api` has not been verified against the real app
- All times should be in IST (Asia/Kolkata) — the users are Indian lawyers
- Keep the Telegram bot simple and text-based — lawyers want quick info, not complex UI
- The single-page case view should be mobile-first — lawyers check on their phones
- WhatsApp messages require pre-approved templates registered with Meta — register them before going live

## Build Status

Steps 1–10 are implemented. Where the code differs from the plan above:

- **Account linking**: `/link <phone>` alone would let anyone link a chat to another lawyer's account, so linking goes through Telegram's "share contact" button; the bot checks the contact belongs to the sender. First-time users get an account created this way, since the bot is the MVP interface. Extra commands: `/track <CNR>`, `/untrack <CNR>`.
- **OTP**: without an SMS gateway, `POST /auth/send-otp` returns 503 unless `DEBUG=true` (then it logs the code and any 6-digit OTP verifies, per the spec). Wrong codes are limited to `OTP_MAX_ATTEMPTS`; resends have a 60s cooldown.
- **Schedule**: the 3/2-day reminders and weekly digest go out at each user's own `notification_time`/`digest_day` (defaults 08:00, Monday), checked every 15 minutes by beat; the 1-day reminder is sent to everyone at `REMINDER_EVENING_TIME`. Every send is deduplicated in Redis (`notif:*` keys).
- **Polling**: `poll_all_cases` enqueues one `poll_case` per due CNR; a Redis fixed-window limiter enforces `ECOURTS_RATE_LIMIT_PER_MINUTE` across all workers. Failures back off exponentially after 3 in a row; disposed cases are re-polled weekly.
- **Plans**: `POST /users/me/plan/upgrade` changes the tier for 30 days with no payment. Put a payment gateway in front of it before launch. Expired plans (and the WhatsApp add-on) fall back to Free automatically.
- **Case IDs**: `/cases/{case_id}` uses `CourtCase.id`, scoped to the caller's tracked cases.

Shared helpers: `scraper/persist.py` maps scraper dicts ↔ `CourtCase` rows (both the API and the poller use it, so diffs compare like with like); `app/cases/service.py` holds tracking logic shared by the API and the bot; `notifications/content.py` builds per-channel content.

## Tests

```bash
pip install -r requirements-dev.txt
pytest   # needs PostgreSQL; set TEST_DATABASE_URL (default postgresql://courtpilot:courtpilot@localhost:5432/courtpilot_test)
```

Redis is faked with fakeredis. eCourts is faked in `tests/conftest.py::FakeScraper`, which builds real `bharat_courts` `CaseDetail` objects so the SDK → model mapping is exercised.
