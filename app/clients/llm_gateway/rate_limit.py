"""Reserve the group's token window before a call and audit actual gateway usage."""

import asyncio
import fcntl
import json
import math
import os
import time
from pathlib import Path

from .exceptions import (
    LLMGatewayBudgetExceededError,
    LLMGatewayHTTPError,
    LLMGatewayRateLimitError,
)


class LLMGatewayRateLimiter:
    def __init__(self, path: Path, interval_seconds: float = 60) -> None:
        if interval_seconds < 60 or not math.isfinite(interval_seconds):
            raise ValueError("Reserve at least one full LLM token window")
        self.path = path
        self.interval = interval_seconds

    def _update(self, *, timeout=None, usage=None, failure=None) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LLMGatewayRateLimitError(1) from None
            raw = file.read()
            state = json.loads(raw) if raw else {}
            if not isinstance(state, dict):
                raise ValueError("Invalid LLM quota state")
            next_allowed = float(state.get("next_allowed", 0))
            if not math.isfinite(next_allowed):
                raise ValueError("Invalid LLM quota state")
            now = time.time()
            if timeout is not None:
                if state.get("budget_exhausted"):
                    raise LLMGatewayBudgetExceededError(state["budget_exhausted"])
                if next_allowed > now:
                    raise LLMGatewayRateLimitError(next_allowed - now)
                # Reserve all 1000 tokens, not a guessed tokenizer count. Also
                # cover the call's duration so completions cannot bunch together.
                state["next_allowed"] = now + timeout + self.interval
                state["requests"] = state.get("requests", 0) + 1
            else:
                state["next_allowed"] = max(next_allowed, now + self.interval)
                if isinstance(usage, dict) and all(
                    type(usage.get(key)) is int and usage[key] >= 0
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                ):
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        state[key] = state.get(key, 0) + usage[key]
                    state["last_usage"] = {
                        key: usage[key]
                        for key in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        )
                    }
                else:
                    state["unknown_usage_requests"] = (
                        state.get("unknown_usage_requests", 0) + 1
                    )
                if failure is not None:
                    state["last_error"] = type(failure).__name__
                    if isinstance(failure, LLMGatewayBudgetExceededError):
                        state["budget_exhausted"] = failure.status_code
                    if isinstance(failure, LLMGatewayHTTPError):
                        delay = failure.retry_after_seconds
                        if delay is not None and math.isfinite(delay):
                            state["next_allowed"] = max(
                                state["next_allowed"], now + delay
                            )
            file.seek(0)
            file.truncate()
            json.dump(state, file)
            file.flush()

    async def acquire(self, timeout_seconds: float) -> None:
        try:
            await asyncio.to_thread(self._update, timeout=timeout_seconds)
        except (OSError, ValueError, TypeError, KeyError):
            raise LLMGatewayRateLimitError(60) from None

    async def record(self, usage, failure=None) -> None:
        try:
            await asyncio.to_thread(self._update, usage=usage, failure=failure)
        except (OSError, ValueError, TypeError, KeyError):
            raise LLMGatewayRateLimitError(60) from None
