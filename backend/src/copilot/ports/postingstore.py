from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from copilot.domain.posting import Posting


class PostingStorePort(Protocol):
    """Remember postings across runs.

    Without this the pipeline is amnesiac: it refetches ~25k postings, shows you
    the same roles every day, cannot say what is *new*, cannot tell when a role
    closes, and re-pays the LLM for descriptions it already read. Persistence is
    what turns a fetch script into a daily product.
    """

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        """Upsert a fetch. Returns ``(newly_seen_ids, already_known_ids)``."""
        ...

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        """Mark postings absent from this fetch as closed. Returns how many."""
        ...

    def new_since(self, since: datetime) -> list[Posting]:
        """Postings first seen after ``since`` — the 'what changed' feed."""
        ...

    def open_postings(self) -> list[Posting]:
        """Everything not yet marked closed."""
        ...

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        """A previously stored LLM result, or ``None``. The main cost lever."""
        ...

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        """Store an LLM result so the description is never re-read."""
        ...

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        """Record that a human applied — the handoff into the application pipeline."""
        ...
