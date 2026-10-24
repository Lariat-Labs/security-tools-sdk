"""Client-side rate limiting so we stay inside each upstream's published limits."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Allow at most ``calls`` requests in any rolling ``period`` seconds."""

    def __init__(self, calls: int, period: float) -> None:
        self.calls = calls
        self.period = period
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self.period:
                    self._stamps.popleft()
                if len(self._stamps) < self.calls:
                    self._stamps.append(now)
                    return
                wait = self.period - (now - self._stamps[0])
            time.sleep(max(wait, 0.01))
