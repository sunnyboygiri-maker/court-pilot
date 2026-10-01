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

# Run with Docker (API, worker, beat, Postgres, Redis; migrations run on API start)
docker compose up -d
# Development without a public webhook URL: also run the bot in polling mode
docker compose --profile polling up -d bot

# Or run locally (needs Postgres + Redis)
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
celery -A workers.celery_app worker -l info
celery -A workers.celery_app beat -l info
python -m bot.telegram_bot          # polling mode, if TELEGRAM_WEBHOOK_URL is unset
```

API docs: http://localhost:8000/docs · Health: `/health` · Case page: `/case/<CNR>/view`

## Telegram Bot

Lawyers start the bot, tap **Share my phone number** (Telegram verifies it), and their
account is created or linked. Commands: `/track <CNR>`, `/untrack <CNR>`, `/cases`,
`/case <CNR>`, `/upcoming`, `/link <phone>`, `/help`.

Production: set `TELEGRAM_WEBHOOK_URL=https://<domain>/webhook/telegram`; the API
registers the webhook on startup and verifies Telegram's secret-token header.

## Before Going Live

- Integrate an SMS gateway in `app/auth/otp.py::deliver_otp` (MSG91 etc.); until then
  phone OTP login only works with `DEBUG=true`, and Telegram contact-share is the login path.
- Put payment in front of `POST /users/me/plan/upgrade` (it currently just changes the tier).
- Register the WhatsApp templates listed in `notifications/whatsapp.py` with Meta.
- Confirm the eCourts CNR lookup works from your server's network (the portals block some hosting IPs).

## Tests

```bash
pip install -r requirements-dev.txt
pytest   # uses TEST_DATABASE_URL (default: courtpilot_test on localhost Postgres); Redis is faked
```
