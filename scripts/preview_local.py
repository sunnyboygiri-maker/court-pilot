"""
Local preview launcher for CourtPilot (no Docker needed).
- Embedded Postgres via pgserver, fakeredis in place of Redis, DEBUG on
- Seeds a demo lawyer with 5 cases; runs uvicorn on 127.0.0.1:8010
Needs `pip install pgserver fakeredis`. Run with the project's venv:
    .venv/Scripts/python scripts/preview_local.py
Demo data lives in .preview/ (git-ignored); delete that folder to reset it.
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HERE = PROJECT / ".preview"
HERE.mkdir(exist_ok=True)
PGDATA = HERE / "pgdata"
sys.path.insert(0, str(PROJECT))

import pgserver  # noqa: E402

# Postgres keeps its start directory open; use .preview/ so the project root stays movable
os.chdir(HERE)
srv = pgserver.get_server(str(PGDATA), cleanup_mode=None)
os.chdir(PROJECT)
admin_uri = srv.get_uri()
if "courtpilot" not in [line.strip() for line in srv.psql("SELECT datname FROM pg_database;").splitlines()]:
    srv.psql("CREATE DATABASE courtpilot;")
db_uri = admin_uri.rsplit("/", 1)[0] + "/courtpilot"

os.environ.update(
    DATABASE_URL=db_uri,
    DEBUG="true",
    SECRET_KEY="local-preview-only-secret-local-preview-only",
    TELEGRAM_BOT_TOKEN="",
    # Your real bot's username (without @), so the pages show it; set it before
    # running, e.g.  set PREVIEW_BOT_USERNAME=MyBot  — otherwise no name is shown
    TELEGRAM_BOT_USERNAME=os.environ.get("PREVIEW_BOT_USERNAME", ""),
    TELEGRAM_WEBHOOK_URL="",
    APP_BASE_URL="http://127.0.0.1:8010",
    # No Celery here: "Find a case" searches run inside this process
    SEARCH_INLINE="true",
)

import fakeredis  # noqa: E402
import fakeredis.aioredis  # noqa: E402

import app.redis as app_redis  # noqa: E402
import workers.redis_client as worker_redis  # noqa: E402

_fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
app_redis.get_redis = lambda: _fake
_fake_sync = fakeredis.FakeRedis(decode_responses=True)
worker_redis.get_sync_redis = lambda: _fake_sync

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth import otp  # noqa: E402
from app.main import app  # noqa: E402

# Behave like production for numbers not linked to Telegram (DEBUG would hand them a code)
_issue = otp.issue_otp


async def _issue_like_production(redis, phone, telegram_chat_id=None):
    if not telegram_chat_id:
        raise otp.OTPNoChannel("Link your Telegram account to receive login codes")
    return await _issue(redis, phone, telegram_chat_id)


otp.issue_otp = _issue_like_production
from models.database import (  # noqa: E402
    Base, CaseSnapshot, CaseStatus, CourtCase, CourtType, PlanTier, TrackedCase, User,
)
from models.session import sync_database_url  # noqa: E402

eng = create_engine(sync_database_url(db_uri))
Base.metadata.create_all(eng)
today = date.today()
ecourts = "https://services.ecourts.gov.in/"

with Session(eng) as s:
    if s.scalar(select(User).where(User.phone == "+919800000001")) is None:
        user = User(phone="+919800000001", name="Adv. Priya Sharma", email="priya@example.com",
                    bar_registration_no="D/1234/2015", plan=PlanTier.FREE, max_cases=5,
                    telegram_chat_id="demo-chat", telegram_username="priyasharma_adv")
        s.add(user)

        c1 = CourtCase(
            cnr_number="DLWE010012342024", case_type="CS (Comm)", case_number="412/2024",
            filing_year=2024, filing_date=date(2024, 2, 12), registration_date=date(2024, 2, 19),
            court_type=CourtType.DISTRICT, court_name="District Judge (Commercial)-03, West",
            court_complex="Tis Hazari Courts Complex", state="Delhi", district="West",
            petitioner="Sharma Textiles Pvt. Ltd.", respondent="Kumar Logistics LLP",
            petitioner_advocate="Adv. Priya Sharma", respondent_advocate="Adv. R. Mehta",
            status=CaseStatus.PENDING, next_hearing_date=today + timedelta(days=3),
            previous_hearing_date=today - timedelta(days=24), stage="Evidence",
            judge="Sh. Arvind Kumar, DJ (Comm)-03",
            acts_sections=[{"act": "Commercial Courts Act, 2015", "section": "12A"},
                           {"act": "Code of Civil Procedure, 1908", "section": "Order XXXVII"}],
            latest_order_date=today - timedelta(days=24),
            orders_json=[
                {"date": str(today - timedelta(days=24)), "description": "Interim order, PW1 cross-examined", "link": ecourts},
                {"date": str(today - timedelta(days=71)), "description": "Issues framed", "link": ecourts},
                {"date": "2024-04-03", "description": "Summons issued", "link": ecourts},
            ],
            raw_ecourts_data={"history": [
                {"business_date": str(today - timedelta(days=24)), "purpose": "Evidence", "judge": "DJ (Comm)-03"},
                {"business_date": str(today - timedelta(days=71)), "purpose": "Framing of issues", "judge": "DJ (Comm)-03"},
                {"business_date": str(today - timedelta(days=130)), "purpose": "Admission/denial", "judge": "DJ (Comm)-03"},
                {"business_date": "2024-04-03", "purpose": "Appearance", "judge": "DJ (Comm)-03"},
            ]},
            last_polled_at=datetime.utcnow() - timedelta(hours=2),
        )
        c2 = CourtCase(
            cnr_number="DLHC010582482024", case_type="W.P.(C)", case_number="8841/2024",
            filing_year=2024, filing_date=date(2024, 6, 20), court_type=CourtType.HIGH_COURT,
            court_name="High Court of Delhi", bench="Hon'ble Ms. Justice A. Rao", state="Delhi",
            petitioner="Ritu Malhotra", respondent="Union of India & Ors.",
            petitioner_advocate="Adv. Priya Sharma", status=CaseStatus.PENDING,
            next_hearing_date=today + timedelta(days=1), stage="Arguments",
            judge="Hon'ble Ms. Justice A. Rao", orders_json=[],
            last_polled_at=datetime.utcnow() - timedelta(hours=5),
        )
        c3 = CourtCase(
            cnr_number="MHPU020045672023", case_type="RCS", case_number="1203/2023",
            court_type=CourtType.DISTRICT, court_name="Civil Judge Senior Division, Pune",
            state="Maharashtra", district="Pune", petitioner="Deshpande Housing Society",
            respondent="Pune Municipal Corporation", status=CaseStatus.DISPOSED,
            stage="Decided", judge="Smt. P. Kulkarni",
            orders_json=[{"date": "2025-11-14", "description": "Final judgment", "link": ecourts}],
        )
        c4 = CourtCase(
            cnr_number="DLST020034512025", case_type="Bail Appln.", case_number="2210/2025",
            court_type=CourtType.DISTRICT, court_name="Addl. Sessions Judge-04, South",
            court_complex="Saket Courts Complex", state="Delhi", district="South",
            petitioner="Vikram Singh", respondent="State (NCT of Delhi)", status=CaseStatus.PENDING,
            next_hearing_date=today, stage="Arguments on bail", judge="Ms. Neha Gupta, ASJ-04",
            orders_json=[], last_polled_at=datetime.utcnow() - timedelta(hours=1),
        )
        c5 = CourtCase(
            cnr_number="HRGR010078902024", case_type="CS", case_number="88/2024",
            court_type=CourtType.DISTRICT, court_name="Civil Judge (Sr. Div.), Gurugram",
            state="Haryana", district="Gurugram", petitioner="Anand Builders Pvt. Ltd.",
            respondent="M/s Greenfield Interiors", status=CaseStatus.PENDING,
            next_hearing_date=today + timedelta(days=19), stage="Written statement",
            judge="Sh. R. K. Yadav", orders_json=[], last_polled_at=datetime.utcnow() - timedelta(hours=3),
        )
        s.add_all([c1, c2, c3, c4, c5])
        s.flush()
        for c, label, client, prio in [
            (c1, "Sharma recovery suit", "Sharma Textiles", 1),
            (c2, "Malhotra writ", "Ritu Malhotra", 2),
            (c3, "Society matter", "Deshpande Housing Society", 0),
            (c4, None, "Vikram Singh", 2),
            (c5, None, "Anand Builders", 0),
        ]:
            s.add(TrackedCase(user_id=user.id, case_id=c.id, label=label, client_name=client, priority=prio,
                              notes="Carry certified copies of Ex. P-4 to P-9." if c is c1 else None))
        s.add(CaseSnapshot(case_id=c1.id, data_hash="demo", snapshot_data={},
                           changes={"next_hearing_date": {"old": str(today - timedelta(days=24)),
                                                          "new": str(today + timedelta(days=3))}}))
        s.commit()

PORT = 8010

if __name__ == "__main__":
    import socket

    import uvicorn

    # One socket for IPv4 and IPv6, so both http://localhost and http://127.0.0.1 work
    sock = socket.create_server(("::", PORT), family=socket.AF_INET6, dualstack_ipv6=True)
    print(f"\nCourtPilot is running. Open http://localhost:{PORT} in your browser.", flush=True)
    print("Demo login: mobile 98000 00001 (the code is shown on screen).", flush=True)
    print("Close this window to stop it.\n", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
