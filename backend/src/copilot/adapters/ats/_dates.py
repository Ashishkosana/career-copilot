"""Timestamp coercion. Every ATS spells "when was this posted" differently."""
from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` and bare dates."""
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_epoch_ms(raw: object) -> datetime | None:
    """Parse a millisecond epoch (Lever's ``createdAt``)."""
    if not isinstance(raw, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
