"""Observe existing API responses without making requests or changing retries."""

import logging
import time

import httpx

logger = logging.getLogger(__name__)


class RateLimitDiagnostics:
    def __init__(self, service: str) -> None:
        self.service = service
        self._last_logged_at: float | None = None
        self._responses = 0
        self._authentication = 0
        self._limited = 0

    def observe(self, response: httpx.Response) -> None:
        self._responses += 1
        self._authentication += response.request.url.path.endswith("/autenticar")
        self._limited += response.status_code == 429
        now = time.monotonic()
        if (
            response.status_code != 429
            and self._last_logged_at is not None
            and now - self._last_logged_at < 30
        ):
            return
        # Never include request headers, query strings, cookies or response bodies.
        headers = {
            name: response.headers[name]
            for name in (
                "date",
                "retry-after",
                "ratelimit",
                "ratelimit-policy",
                "ratelimit-limit",
                "ratelimit-remaining",
                "ratelimit-reset",
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
            )
            if name in response.headers
        }
        logger.info(
            "API quota observation: service=%s host=%s responses=%s "
            "authentication=%s limited=%s elapsed_seconds=%.1f headers=%s",
            self.service,
            response.request.url.host,
            self._responses,
            self._authentication,
            self._limited,
            0 if self._last_logged_at is None else now - self._last_logged_at,
            headers,
        )
        self._last_logged_at = now
        self._responses = self._authentication = self._limited = 0
