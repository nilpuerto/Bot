"""Retry helpers built on ``tenacity`` for HTTP-flaky services."""
from __future__ import annotations

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import httpx


def http_retrying(attempts: int = 3, base_wait: float = 0.5) -> AsyncRetrying:
    """Exponential-backoff retry for transient HTTP errors."""
    return AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=base_wait, min=base_wait, max=8.0),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)
        ),
    )
