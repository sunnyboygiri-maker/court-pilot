from redis.asyncio import Redis

from config.settings import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """FastAPI dependency; one client (and connection pool) per process."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
