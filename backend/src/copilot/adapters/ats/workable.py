"""Workable adapters — cross-company meta-search plus a per-company widget.

The meta-search endpoint is the only source in this package that needs **no
company slug**: it queries every Workable board at once. That makes it the one
adapter which finds companies rather than requiring you to already know them, so
it doubles as watchlist-discovery input.

Two honest caveats:

* The ``url`` is a ``jobs.workable.com/view/…`` page, not the employer's own
  domain. It is still the ATS's real posting page (apply happens there), but it
  is not a direct employer link the way Greenhouse's ``absolute_url`` is.
* ``description`` is raw HTML here, unlike Lever's plain text.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from copilot.adapters.ats._dates import parse_iso
from copilot.adapters.ats._http import get_json
from copilot.adapters.ats._text import html_to_text
from copilot.domain.posting import Posting

ATS = "workable"
_SEARCH = "https://jobs.workable.com/api/v1/jobs"
_WIDGET = "https://apply.workable.com/api/v1/widget/accounts/{tenant}?details=true"

# Workable caps the meta-search page size server-side; this is a safety bound on
# how many pages we will walk in one run, not a page size.
_MAX_PAGES = 25


def _location_text(job: dict[str, Any]) -> str:
    loc = job.get("location")
    if not isinstance(loc, dict):
        return str(loc or "")
    parts = [str(loc.get(k) or "") for k in ("city", "subregion", "countryName")]
    return ", ".join(p for p in parts if p)


def parse_search(payload: Any) -> tuple[list[Posting], str]:
    """Map one meta-search page onto postings plus the next page token (pure)."""
    if not isinstance(payload, dict):
        return [], ""
    out: list[Posting] = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        url = str(job.get("url") or "")
        title = str(job.get("title") or "")
        if not url or not title:
            continue
        company = job.get("company")
        description = html_to_text(job.get("description"))
        workplace = str(job.get("workplace") or "").lower()
        out.append(
            Posting(
                title=title,
                company=str((company or {}).get("title") or "")
                if isinstance(company, dict)
                else str(company or ""),
                url=url,
                ats=ATS,
                location=_location_text(job),
                description=description,
                desc_available=bool(description),
                req_id=str(job.get("id") or ""),
                posted_at=parse_iso(job.get("created")) or parse_iso(job.get("updated")),
                remote=True if workplace == "remote" else (False if workplace else None),
                employment_type=str(job.get("employmentType") or ""),
            )
        )
    return out, str(payload.get("nextPageToken") or "")


def parse_widget(payload: Any, tenant: str) -> list[Posting]:
    """Map a per-company widget payload onto postings (pure)."""
    if not isinstance(payload, dict):
        return []
    company = str(payload.get("name") or tenant)
    out: list[Posting] = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        url = str(job.get("application_url") or job.get("url") or job.get("shortlink") or "")
        title = str(job.get("title") or "")
        if not url or not title:
            continue
        description = html_to_text(job.get("description")) or html_to_text(
            job.get("full_description")
        )
        out.append(
            Posting(
                title=title,
                company=company,
                url=url,
                ats=ATS,
                tenant=tenant,
                location=str(job.get("location") or job.get("city") or ""),
                description=description,
                desc_available=bool(description),
                req_id=str(job.get("id") or job.get("shortcode") or ""),
                posted_at=parse_iso(job.get("published_on")) or parse_iso(job.get("created_at")),
            )
        )
    return out


class WorkableSearchSource:
    """PostingSourcePort over Workable's cross-company meta-search.

    Pagination is an opaque cursor: the response's ``nextPageToken`` goes back as
    the ``pageToken`` query parameter — camelCase, and verified by probing, since
    it is undocumented. The snake_case spelling is silently ignored and re-serves
    page one, so ``page_token_param`` stays configurable and pagination stops as
    soon as a page fails to advance rather than looping.
    """

    name = ATS

    def __init__(
        self,
        query: str = "software engineer",
        *,
        location: str = "United States",
        day_range: int = 30,
        max_pages: int = _MAX_PAGES,
        page_token_param: str = "pageToken",
    ) -> None:
        self._query = query
        self._location = location
        self._day_range = day_range
        self._max_pages = max(1, min(max_pages, _MAX_PAGES))
        self._token_param = page_token_param

    def _url(self, token: str) -> str:
        params: dict[str, str] = {
            "query": self._query,
            "location": self._location,
            "day_range": str(self._day_range),
        }
        if token:
            params[self._token_param] = token
        return f"{_SEARCH}?{urllib.parse.urlencode(params)}"

    def fetch(self) -> list[Posting]:
        collected: list[Posting] = []
        seen_ids: set[str] = set()
        token = ""
        for _ in range(self._max_pages):
            page, token = parse_search(get_json(self._url(token)))
            fresh = [p for p in page if p.id not in seen_ids]
            if not fresh:
                break  # token did not advance (or unsupported) — stop cleanly
            seen_ids.update(p.id for p in fresh)
            collected.extend(fresh)
            if not token:
                break
        return collected


class WorkableWidgetSource:
    """PostingSourcePort over one company's Workable widget."""

    name = ATS

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant

    def fetch(self) -> list[Posting]:
        return parse_widget(get_json(_WIDGET.format(tenant=self._tenant)), self._tenant)
