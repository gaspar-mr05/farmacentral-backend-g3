"""Shared pacing for clients using one local file, including systemd workers."""

import asyncio
import fcntl
import math
import os
import time
from pathlib import Path

from .exceptions import BillingRateLimitError


class BillingRateLimiter:
    def __init__(self, path: Path, requests_per_minute: int) -> None:
        if not 1 <= requests_per_minute <= 60:
            raise ValueError("Billing request rate must be between 1 and 60")
        self.path = path
        self.interval = 60 / requests_per_minute

    def _reserve(self, cooldown: float = 0) -> float:
        # Never hold this lock during network calls or sleeps. O_NOFOLLOW avoids
        # following a different user's symlink at a predictable /tmp path.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+", encoding="ascii") as state:
            try:
                fcntl.flock(state.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0.05
            raw = state.read(128)
            next_allowed = float(raw) if raw else 0
            if not math.isfinite(next_allowed):
                raise ValueError("Invalid billing pacing state")
            now = time.time()
            wait = max(0, next_allowed - now)
            if cooldown:
                next_allowed = max(next_allowed, now + cooldown)
            elif wait:
                return wait
            else:
                next_allowed = now + self.interval
            state.seek(0)
            state.truncate()
            state.write(str(next_allowed))
            state.flush()
            return 0

    async def acquire(self) -> None:
        try:
            async with asyncio.timeout(65):
                while delay := await asyncio.to_thread(self._reserve):
                    if delay > 65:
                        raise BillingRateLimitError(
                            "Billing cooldown is active; no request sent",
                            retry_after_seconds=delay,
                        )
                    await asyncio.sleep(min(delay, 1))
        except (OSError, ValueError, TimeoutError):
            raise BillingRateLimitError(
                "Billing pacing unavailable; no request sent"
            ) from None

    async def defer(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if not math.isfinite(seconds):
            raise BillingRateLimitError("Invalid billing cooldown")
        try:
            # _reserve can report a busy lock; retry asynchronously, not while
            # holding a worker thread or resetting another process's cooldown.
            async with asyncio.timeout(5):
                while delay := await asyncio.to_thread(self._reserve, seconds):
                    await asyncio.sleep(min(delay, 0.05))
        except (OSError, ValueError, TimeoutError):
            raise BillingRateLimitError("Could not persist billing cooldown") from None
