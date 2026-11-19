"""Pace the course gateway across clients and systemd processes on one host."""

import asyncio
import fcntl
import hashlib
import logging
import math
import os
import tempfile
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class SharedQuotaTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        group: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        directory: Path | None = None,
    ) -> None:
        self._group = group
        self._transport = transport or httpx.AsyncHTTPTransport()
        # /tmp is private to each systemd unit (PrivateTmp=true).
        shared = Path("/var/lib/farmacentral")
        self._directory = directory or (
            shared if shared.is_dir() else Path(tempfile.gettempdir())
        )

    def _update(self, host: str, cooldown: float = 0) -> float:
        scope = hashlib.sha256(host.encode()).hexdigest()[:12]
        path = self._directory / f"gateway-{os.getuid()}-{self._group}-{scope}.rate"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+", encoding="ascii") as state:
            try:
                fcntl.flock(state.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0.05
            raw = state.read(128)
            next_allowed = float(raw) if raw else 0
            if not math.isfinite(next_allowed):
                raise ValueError("Invalid shared gateway pacing state")
            now = time.time()
            if cooldown:
                next_allowed = max(next_allowed, now + cooldown)
            elif next_allowed > now:
                return next_allowed - now
            else:
                # 200/minute leaves margin below the observed 250/minute quota.
                next_allowed = now + 0.3
            state.seek(0)
            state.truncate()
            state.write(str(next_allowed))
            state.flush()
        return 0

    async def _wait(self, host: str, cooldown: float = 0) -> None:
        while delay := await asyncio.to_thread(self._update, host, cooldown):
            await asyncio.sleep(min(delay, 1))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            await self._wait(host)
        except (OSError, ValueError) as exc:
            raise httpx.ConnectError(
                "Shared gateway pacing unavailable; no request sent", request=request
            ) from exc
        response = await self._transport.handle_async_request(request)
        if response.status_code == 429:
            try:
                await self._wait(host, _cooldown(response))
            except (OSError, ValueError):
                logger.warning("Could not share gateway cooldown: host=%s", host)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def _cooldown(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "60")
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 60
    return max(0.3, seconds) if math.isfinite(seconds) else 60
