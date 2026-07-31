"""The ``(company, ats, tenant)`` watchlist.

No API in this package enumerates tenants, so which companies to poll is an input
we have to maintain rather than discover. Two things feed it: Simplify's public
``listings.json`` (classify the apply-URL hostname) and occasional
site-restricted searches such as
``site:myworkdayjobs.com "junior software developer"``, which is a *slug factory*
— every hit hands you a tenant identifier.

Stored as JSON rather than YAML purely to avoid adding ``pyyaml`` to a bundle that
is assembled by hand.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, field_validator

from copilot.domain.demo_boards import is_demo_tenant

SUPPORTED_ATS = frozenset({"greenhouse", "ashby", "lever", "workable", "workday"})


class WatchlistEntry(BaseModel):
    """One board to poll."""

    model_config = {"frozen": True}

    company: str
    ats: str
    tenant: str
    # Workday only: the wdN shard and the career-site slug.
    wd: str = ""
    site: str = ""

    @field_validator("ats")
    @classmethod
    def _known_ats(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_ATS:
            raise ValueError(f"unsupported ats {value!r}; expected one of {sorted(SUPPORTED_ATS)}")
        return normalized

    @property
    def is_complete(self) -> bool:
        """Workday needs a shard and a site slug; the others just need a tenant."""
        if self.ats == "workday":
            return bool(self.tenant and self.wd and self.site)
        return bool(self.tenant)


def parse_watchlist(payload: object, *, allow_demo: bool = False) -> list[WatchlistEntry]:
    """Validate a decoded watchlist document (pure — no filesystem).

    ``allow_demo`` exists so the adapters' own tests can point at a demo board
    deliberately; nothing in the product sets it.
    """
    rows: Iterable[object]
    if isinstance(payload, dict):
        rows = payload.get("companies") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    entries: list[WatchlistEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            entry = WatchlistEntry.model_validate(row)
        except ValueError:
            continue
        if not entry.is_complete:
            continue
        if not allow_demo and is_demo_tenant(entry.tenant):
            continue
        entries.append(entry)
    return entries


def load_watchlist(path: str | Path) -> list[WatchlistEntry]:
    """Read and validate a watchlist file. Missing file → empty list."""
    target = Path(path)
    if not target.exists():
        return []
    return parse_watchlist(json.loads(target.read_text()))
