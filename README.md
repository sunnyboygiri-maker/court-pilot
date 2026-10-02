# CourtPilot — Case Tracking & Notification Platform for Indian Lawyers

## Overview

CourtPilot is a SaaS platform that helps Indian litigation lawyers track their court cases and receive timely notifications about upcoming hearings, new orders, and case updates. It scrapes data from India's eCourts system and delivers notifications via Telegram (free), Email (free), and WhatsApp (premium).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     COURTPILOT                          │
│                                                         │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │ FastAPI   │   │   Celery     │   │  Notification  │  │
│  │ Backend   │◄─►│   Workers    │──►│  Pipeline      │  │
│  │           │   │              │   │                │  │
│  │ • Auth    │   │ • Poll cases │   │ • Telegram Bot │  │
│  │ • CRUD    │   │ • Diff check │   │ • Email (SMTP) │  │
│  │ • Plans   │   │ • Schedule   │   │ • WhatsApp API │  │
│  └─────┬─────┘   └──────┬───────┘   └────────────────┘  │
│        │                │                                │
│        ▼                ▼                                │
│  ┌──────────────────────────────┐                       │
│  │        PostgreSQL            │                       │
│  │  • Users & subscriptions     │                       │
│  │  • Tracked cases             │                       │
│  │  • Case snapshots & diffs    │                       │
│  │  • Notification log          │                       │
│  └──────────────────────────────┘                       │
│        │                                                │
│        ▼                                                │
│  ┌──────────────────────────────┐                       │
│  │        Redis                 │                       │
│  │  • Celery task broker        │                       │
│  │  • Case dedup cache          │                       │
│  │  • Rate limiting counters    │                       │
│  └──────────────────────────────┘                       │
└─────────────────────────────────────────────────────────┘

External:
  ┌──────────────────┐
  │ eCourts Services │  ← polled via bharat-courts SDK
  │ (NIC servers)    │    (district + HC + SC)
  └──────────────────┘
```

## Pricing Tiers

| Plan       | Cases | Channels           | Price/month |
|------------|-------|--------------------|-------------|
| Free       | 5     | Telegram + Email   | ₹0          |
| Starter    | 50    | Telegram + Email   | ₹299        |
| Pro        | 100   | Telegram + Email   | ₹499        |
| Firm       | 500   | Telegram + Email   | ₹1,499      |
| WhatsApp+  | —     | Add WhatsApp       | +₹199/mo    |

## Notification Schedule

- **Weekly digest**: Every Monday 8 AM — all upcoming hearings for the week
- **3-day reminder**: 3 days before hearing date
- **2-day reminder**: 2 days before hearing date
- **1-day reminder**: 1 day before hearing date (evening)
- **New order alert**: Whenever a new order/judgment is uploaded

## Tech Stack

- **Backend**: Python 3.12, FastAPI
- **Database**: PostgreSQL 16
- **Queue**: Celery + Redis
- **Scraping**: bharat-courts SDK (eCourts API)
- **Telegram**: python-telegram-bot
- **WhatsApp**: Meta Cloud API
- **Email**: aiosmtplib + Jinja2 templates
- **Deployment**: Docker + docker-compose

## Quick Start

```bash
cp .env.example .env
# Edit .env: at minimum SECRET_KEY and TELEGRAM_BOT_TOKEN

# Run with Docker (API, notification worker, poller, beat, Postgres, Redis;
# migrations run on API start)
docker compose up -d
# Development without a public webhook URL: also run the bot in polling mode
docker compose --profile polling up -d bot

# Or run locally (needs Postgres + Redis)
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
celery -A workers.celery_app worker -Q celery -l info    # reminders & alerts
celery -A workers.celery_app worker -Q polling -l info   # eCourts polling
celery -A workers.celery_app worker -Q search -l info    # "Find a case" searches
celery -A workers.celery_app beat -l info
python -m bot.telegram_bot          # polling mode, if TELEGRAM_WEBHOOK_URL is unset
```

Web app: http://localhost:8000 · Health: `/health` · Public case page: `/case/<CNR>/view`
· API docs: `/docs` (only when `DEBUG=true`)

## Web App

A mobile-first site served by the same FastAPI app (Jinja2 + htmx, no build step):
`/login`, `/` (hearings grouped Today / Tomorrow / This week / Later), `/cases`,
`/cases/new`, `/cases/<id>/view`, `/settings`. Code lives in `app/web/`,
templates in `app/templates/web/`, styles in `app/static/`.

- **Login**: "Continue with Google", "Continue with email" or mobile number, all
  passwordless and all creating the account on first use.
  - Phone: 6-digit code by SMS when `SMS_PROVIDER` is set (2Factor or MSG91), otherwise
    on Telegram to the chat linked to that number.
  - Email: 6-digit code by SMTP. Only *confirmed* addresses log in or get reminders;
    changing the email in Settings sends a confirmation code first.
  - Google: OAuth (`GOOGLE_CLIENT_ID/SECRET`), needs https on a domain. Google and email
    with the same address reach the same account.
  - Accounts without a phone connect Telegram with a one-tap link in Settings
    (`t.me/<bot>?start=link_<token>`); sharing their number in the bot then adds it.
  - Buttons for Google/email only show when configured (always in DEBUG).
- **Add a case** (`/cases/new`): by CNR, or without one, by party name (any spelling),
  case number, FIR, or "My cases" (imports an advocate's cases from eCourts). Searches
  run in the background with live progress; results are ranked and added with one tap.
  Courts are saved per lawyer in "My courts" (Settings).
- **Session**: httpOnly cookie (Secure on HTTPS, SameSite=Lax), 30 days. API bearer
  tokens and web cookies are not interchangeable. Every form carries a CSRF token.
- The web routes are mounted before the API, so `/cases/new` isn't read as
  `GET /cases/{case_id}`; the case page is `/cases/<id>/view` to keep the API's
  `/cases/<id>` free.

## Telegram Bot

Lawyers start the bot, tap **Share my phone number** (Telegram verifies it), and their
account is created or linked. Commands: `/track <CNR>`, `/untrack <CNR>`, `/cases`,
`/case <CNR>`, `/upcoming`, `/link <phone>`, `/help`.

Production: set `TELEGRAM_WEBHOOK_URL=https://<domain>/webhook/telegram`; the API
registers the webhook on startup and verifies Telegram's secret-token header.

## Before Going Live

- Serve over HTTPS on a domain (e.g. Caddy in front of port 8000) so session cookies are
  Secure and the Telegram webhook can be used.
- Optional: add an SMS gateway in `app/auth/otp.py::deliver_otp` (MSG91 etc.) for lawyers
  who don't use Telegram. Login codes currently go out on Telegram only.
- Put payment in front of `POST /users/me/plan/upgrade`. It is disabled unless
  `ALLOW_PLAN_SELF_UPGRADE=true`, and the web app shows "Coming soon".
- Register the WhatsApp templates listed in `notifications/whatsapp.py` with Meta.
- Confirm the eCourts CNR lookup works from your server's network (the portals block some hosting IPs).

## Tests

```bash
pip install -r requirements-dev.txt
pytest   # uses TEST_DATABASE_URL (default: courtpilot_test on localhost Postgres); Redis is faked
```
