import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import asyncio


@dataclass
class DeliveryResult:
    ok: bool
    error: Optional[str] = None
    cost_inr: float = 0.0
    provider_message_id: Optional[str] = None


class SlidingWindowLimiter:
    """
    At most `rate` acquisitions per `per` seconds within one process.

    Uses no asyncio primitives, so one instance is safe to share across the
    separate event loops Celery tasks create with asyncio.run().
    """

    def __init__(self, rate: int, per: float = 1.0):
        self.rate = rate
        self.per = per
        self._stamps: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= self.per:
                self._stamps.popleft()
            if len(self._stamps) < self.rate:
                self._stamps.append(now)
                return
            await asyncio.sleep(self.per - (now - self._stamps[0]))
