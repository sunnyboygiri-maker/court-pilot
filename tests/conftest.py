"""
Tests run against a real PostgreSQL database (TEST_DATABASE_URL, default
courtpilot_test on localhost) with fakeredis standing in for Redis.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://courtpilot:courtpilot@localhost:5432/courtpilot_test"
)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DEBUG"] = "false"
os.environ["TELEGRAM_BOT_TOKEN"] = "123:TEST"
os.environ["TELEGRAM_WEBHOOK_URL"] = ""
os.environ["APP_BASE_URL"] = "https://courtpilot.test"
os.environ["SMTP_USER"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""

from datetime import date, timedelta  # noqa: E402
from typing import Optional  # noqa: E402

import fakeredis  # noqa: E402
import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from bharat_courts.models import ActEntry, CaseDetail, CaseOrder, HearingEntry, PartyEntry  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.auth.jwt import create_access_token  # noqa: E402
from app.cases.service import get_scraper  # noqa: E402
from app.main import app  # noqa: E402
from app.redis import get_redis  # noqa: E402
from config.timeutils import today_ist  # noqa: E402
from models.database import PLAN_LIMITS, Base, PlanTier, User  # noqa: E402
from models.session import get_db, make_engine, sync_database_url  # noqa: E402
from scraper.ecourts import CaseNotFoundError, ECourtsScraper  # noqa: E402

DB_URL = sync_database_url(os.environ["DATABASE_URL"])

CNR_A = "DLWE010012342024"  # district (Delhi West)
CNR_B = "MHPU020045672023"  # district (Pune)
CNR_HC = "DLHC010582482024"  # Delhi High Court prefix


def _ensure_database():
    url = make_url(DB_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def sync_engine():
    _ensure_database()
    eng = create_engine(DB_URL)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_tables(sync_engine):
    yield
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def session_factory(sync_engine):
    eng = make_engine(poolclass=NullPool)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def sync_redis():
    return fakeredis.FakeRedis(decode_responses=True)


# --- Fake eCourts ---

def make_detail(
    cnr: str,
    next_hearing: Optional[date] = None,
    orders: int = 1,
    status: str = "Pending",
    stage: str = "Evidence",
    judge: str = "Addl. Sessions Judge-02",
    decided: Optional[date] = None,
) -> CaseDetail:
    today = today_ist()
    return CaseDetail(
        cnr_number=cnr,
        case_type="CS",
        filing_number="1234/2024",
        filing_date=date(2024, 1, 10),
        registration_number="567/2024",
        registration_date=date(2024, 1, 15),
        first_hearing_date=date(2024, 2, 1),
        next_hearing_date=next_hearing,
        decision_date=decided,
        case_stage=stage,
        status=status,
        court_number_and_judge=judge,
        court_name="Tis Hazari Courts, Delhi",
        state="Delhi",
        district="West",
        petitioners=[PartyEntry("Ramesh Kumar", "A. Sharma"), PartyEntry("Suresh Kumar", "A. Sharma")],
        respondents=[PartyEntry("State of NCT of Delhi", "APP")],
        acts=[ActEntry("Indian Penal Code", "420")],
        history=[HearingEntry(hearing_date=today - timedelta(days=10), business_date=today - timedelta(days=40), purpose="Evidence")],
        orders=[
            CaseOrder(order_date=date(2024, 3, i + 1), order_type="Interim Order", pdf_url=f"https://ecourts.example/o{i}.pdf")
            for i in range(orders)
        ],
    )


class FakeScraper(ECourtsScraper):
    """ECourtsScraper with the SDK call replaced by canned CaseDetails."""

    def __init__(self):
        super().__init__()
        self.details: dict[str, CaseDetail] = {}
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def _fetch_via_sdk(self, cnr_number: str) -> dict:
        self.calls.append(cnr_number)
        if cnr_number in self.fail:
            raise RuntimeError("eCourts timed out")
        detail = self.details.get(cnr_number)
        if detail is None:
            raise CaseNotFoundError(f"Case not found for CNR: {cnr_number}")
        court_type = "high_court" if cnr_number[2:4] == "HC" else "district"
        return self._normalize_case_data(self._case_detail_to_raw(detail), court_type)

    async def fetch_case_by_cnr(self, cnr_number: str) -> dict:
        # Skip tenacity's retry sleeps
        return await ECourtsScraper.fetch_case_by_cnr.retry_with(stop=lambda s: True)(self, cnr_number)


@pytest.fixture
def scraper():
    s = FakeScraper()
    in_5 = today_ist() + timedelta(days=5)
    s.details[CNR_A] = make_detail(CNR_A, next_hearing=in_5)
    s.details[CNR_B] = make_detail(CNR_B, next_hearing=today_ist() + timedelta(days=20))
    s.details[CNR_HC] = make_detail(CNR_HC, next_hearing=in_5)
    return s


# --- API client & users ---

@pytest_asyncio.fixture
async def client(session_factory, redis, scraper):
    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_scraper] = lambda: scraper
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_user(db):
    async def _make(phone="+919876543210", plan=PlanTier.FREE, **kwargs) -> User:
        user = User(phone=phone, name=kwargs.pop("name", "Adv. Test"), plan=plan,
                    max_cases=PLAN_LIMITS[plan]["max_cases"], **kwargs)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    return _make


@pytest_asyncio.fixture
async def user(make_user):
    return await make_user()


@pytest.fixture
def auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}
