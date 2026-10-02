import random
import time

from redis import Redis

from config.settings import settings

_redis: Redis | None = None


def get_sync_redis() -> Redis:
    """Sync client for Celery tasks (they run their own short-lived event loops)."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def acquire_rate_slot(redis: Redis, key: str, per_minute: int) -> float:
    """
    Global fixed-window rate limit shared by all workers.
    Returns 0 if a slot was taken, else seconds to wait before retrying.
    """
    window = int(time.time() // 60)
    counter = f"ratelimit:{key}:{window}"
    pipe = redis.pipeline()
    pipe.incr(counter)
    pipe.expire(counter, 120)
    count, _ = pipe.execute()
    if count <= per_minute:
        return 0
    # Spread retries over the next window rather than stampeding at :00
    return (window + 1) * 60 - time.time() + random.uniform(0, 30)


def claim_once(redis: Redis, key: str, ttl_seconds: int) -> bool:
    """True the first time a key is claimed within its TTL; used to dedup notifications."""
    return bool(redis.set(key, 1, nx=True, ex=ttl_seconds))
