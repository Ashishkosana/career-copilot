"""Minimal JSON-over-HTTP helper built on the stdlib.

No ``requests``/``httpx`` dependency: these adapters ship in a Lambda bundle
assembled by ``infra/build-lambda.sh``, and every avoided wheel is one less thing
to vendor. Retries are deliberately conservative — these are other people's
public job boards, so we back off, honour ``Retry-After``, and give up quickly
rather than hammering a tenant that is rate-limiting us.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from copilot.logging import get_logger

_LOG = get_logger("copilot.adapters.ats.http")

USER_AGENT = "career-copilot/2.0 (personal job-search tool; +https://github.com/Ashishkosana)"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Statuses worth a second attempt. 429 is included but only ever retried once,
# and only after honouring Retry-After.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class AtsFetchError(RuntimeError):
    """A job board could not be read. Carries the status when there was one."""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.status = status


def _request(
    url: str, *, data: bytes | None, timeout: float, attempts: int, sleep: Any
) -> bytes:
    headers = dict(_HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = "unknown error"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE or attempt == attempts:
                raise AtsFetchError(url, f"HTTP {exc.code}", exc.code) from exc
            delay = _retry_after(exc) or 2.0 * attempt
            _LOG.warning(
                "ats_retry",
                extra={"extra_fields": {"url": url, "status": exc.code, "delay_s": delay}},
            )
            sleep(delay)
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise AtsFetchError(url, str(exc)) from exc
            sleep(1.0 * attempt)
            last = str(exc)
    raise AtsFetchError(url, last)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return min(30.0, float(raw))
    except ValueError:
        return None


def get_json(
    url: str, *, timeout: float = 20.0, attempts: int = 3, sleep: Any = time.sleep
) -> Any:
    """GET ``url`` and parse JSON. Raises :class:`AtsFetchError` on failure."""
    return json.loads(_request(url, data=None, timeout=timeout, attempts=attempts, sleep=sleep))


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = 20.0,
    attempts: int = 3,
    sleep: Any = time.sleep,
) -> Any:
    """POST ``body`` as JSON and parse the JSON response."""
    payload = json.dumps(body).encode()
    return json.loads(
        _request(url, data=payload, timeout=timeout, attempts=attempts, sleep=sleep)
    )
