import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.auth.router import router as auth_router
from app.cases.router import router as cases_router
from app.redis import close_redis, get_redis
from app.users.router import router as users_router
from app.views.router import router as views_router
from app.webhooks.telegram import router as telegram_webhook_router
from config.settings import settings
from models.session import SessionLocal, engine

logging.basicConfig(level=logging.DEBUG if settings.DEBUG else logging.INFO)
logger = logging.getLogger("courtpilot")

DEFAULT_SECRET = "change-me-in-production"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.SECRET_KEY == DEFAULT_SECRET:
        if not settings.DEBUG:
            raise RuntimeError("Set SECRET_KEY before running with DEBUG=false")
        logger.warning("Using the default SECRET_KEY (DEBUG mode only)")
    if settings.DEBUG:
        logger.warning("DEBUG is on: any 6-digit OTP is accepted. Never run production like this.")

    # Opens the pool and fails fast if the DB is unreachable
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    app.state.telegram_app = None
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_WEBHOOK_URL:
        from bot.telegram_bot import start_webhook_bot

        app.state.telegram_app = await start_webhook_bot()

    yield

    if app.state.telegram_app is not None:
        from bot.telegram_bot import stop_webhook_bot

        await stop_webhook_bot(app.state.telegram_app)
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    description="Court case tracking and hearing notifications for Indian lawyers",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(cases_router)
app.include_router(users_router)
app.include_router(telegram_webhook_router)
app.include_router(views_router)


@app.get("/health", tags=["health"])
async def health():
    checks = {}
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e.__class__.__name__}"
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e.__class__.__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if healthy else "degraded", **checks}, status_code=200 if healthy else 503)
