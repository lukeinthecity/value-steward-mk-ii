"""Retry/backoff shared by every Alpaca-facing client.

Ported near-verbatim from value-steward's src/valuesteward/data/alpaca_client.py.
Extracted here rather than left in bars.py because the calendar and broker
clients need identical behavior, and three copies would drift.
"""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)

TRANSIENT_MARKERS = ("429", "too many requests", "500", "502")


def is_transient(exc: Exception) -> bool:
    """True for rate limits and transient server errors, false for auth/logic errors."""

    msg = str(exc).lower()
    return any(marker in msg for marker in TRANSIENT_MARKERS)


def retry_alpaca(retries: int = 3, backoff: float = 1.0) -> Callable:
    """Retry on transient failures with exponential backoff; re-raise everything else.

    Deliberately does NOT retry auth or request-validation errors -- those fail
    identically on every attempt, so retrying only delays the report.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if not is_transient(exc):
                        raise
                    wait = backoff * (2**attempt)
                    logger.warning(
                        "[ALPACA] transient failure, retrying in %ss (%d/%d): %s",
                        wait,
                        attempt + 1,
                        retries,
                        exc,
                    )
                    time.sleep(wait)
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("retry_alpaca exhausted without making an attempt")

        return wrapper

    return decorator
