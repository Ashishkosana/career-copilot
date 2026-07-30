"""Greenhouse job-board adapter.

Documented, unauthenticated, and the richest of the open boards: ``absolute_url``
is a real apply link, ``?content=true`` returns the full description, and both
``first_published`` and ``updated_at`` are ISO timestamps.

One trap: ``content`` is **escaped** markup (``&lt;div&gt;``), so it must go
through :func:`html_to_text` rather than a plain tag-strip.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.ats._dates import parse_iso
from copilot.adapters.ats._http import get_json
from copilot.adapters.ats._text import html_to_text
from copilot.domain.posting import Posting

ATS = "greenhouse"
_BASE = "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true"


def parse(payload: Any, tenant: str) -> list[Posting]:
    """Map a board payload onto postings (pure — no network)."""
    if not isinstance(payload, dict):
        return []
    out: list[Posting] = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        url = str(job.get("absolute_url") or "")
        title = str(job.get("title") or "")
        if not url or not title:
            continue
        description = html_to_text(job.get("content"))
        location = job.get("location")
        location_name = str(location.get("name") or "") if isinstance(location, dict) else ""
        out.append(
            Posting(
                title=title,
                company=str(job.get("company_name") or tenant),
                url=url,
                ats=ATS,
                tenant=tenant,
                location=location_name,
                description=description,
                desc_available=bool(description),
                req_id=str(job.get("requisition_id") or job.get("id") or ""),
                posted_at=parse_iso(job.get("first_published")) or parse_iso(job.get("updated_at")),
            )
        )
    return out


class GreenhouseSource:
    """PostingSourcePort over one Greenhouse board."""

    name = ATS

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant

    def fetch(self) -> list[Posting]:
        return parse(get_json(_BASE.format(tenant=self._tenant)), self._tenant)
