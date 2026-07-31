"""Lever job-board adapter.

Returns full ``descriptionPlain`` in the list response with no pagination, which
makes it the cheapest source per posting. ``createdAt`` is a millisecond epoch.

Null-guard note: a minority of Lever records come back with an empty
``descriptionPlain`` (11 of 389 when this adapter was written). Those must be
tagged ``desc_available=False`` rather than passed along as ``""``, or every
description gate silently accepts them.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.ats._dates import parse_epoch_ms
from copilot.adapters.ats._http import get_json
from copilot.adapters.ats._text import html_to_text
from copilot.domain.posting import Posting

ATS = "lever"
_BASE = "https://api.lever.co/v0/postings/{tenant}?mode=json"


def parse(payload: Any, tenant: str) -> list[Posting]:
    """Map a Lever postings list onto postings (pure — no network)."""
    if not isinstance(payload, list):
        return []
    out: list[Posting] = []
    for job in payload:
        if not isinstance(job, dict):
            continue
        url = str(job.get("hostedUrl") or job.get("applyUrl") or "")
        title = str(job.get("text") or "")
        if not url or not title:
            continue
        categories = job.get("categories")
        cats: dict[str, Any] = categories if isinstance(categories, dict) else {}
        body = str(job.get("descriptionPlain") or "")
        extra = str(job.get("additionalPlain") or "")
        description = "\n\n".join(part for part in (body, extra) if part)
        if not description:
            description = html_to_text(job.get("description"))
        out.append(
            Posting(
                title=title,
                company=tenant,
                url=url,
                ats=ATS,
                tenant=tenant,
                location=str(cats.get("location") or ""),
                description=description,
                desc_available=bool(description),
                req_id=str(job.get("id") or ""),
                posted_at=parse_epoch_ms(job.get("createdAt")),
                remote=str(job.get("workplaceType") or "").lower() == "remote" or None,
                employment_type=str(cats.get("commitment") or ""),
            )
        )
    return out


class LeverSource:
    """PostingSourcePort over one Lever board."""

    name = ATS

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant

    def fetch(self) -> list[Posting]:
        return parse(get_json(_BASE.format(tenant=self._tenant)), self._tenant)
