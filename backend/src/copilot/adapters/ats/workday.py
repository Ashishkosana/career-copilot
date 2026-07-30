"""Workday CXS adapter — the highest-volume ATS, and the most booby-trapped.

Three behaviours this adapter exists to get right:

1. **``limit`` is capped at 20 server-side.** Asking for more is silently ignored.
2. **``total`` is only meaningful at ``offset=0``.** Later pages echo a value that
   cannot be trusted as a loop bound.
3. **Deep offsets wrap instead of ending.** A naive ``while offset < total`` loop
   never terminates — the API keeps returning rows, re-serving earlier pages. We
   therefore stop when a page's ``externalPath`` set has been seen before.

The list response carries **no description**, so every posting from here is
``desc_available=False`` and must be routed to title-only gates. ``postedOn`` is
a human string ("Posted 5 Days Ago"), not a timestamp, so ``posted_at`` is left
unset rather than guessed.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.ats._http import post_json
from copilot.domain.posting import Posting

ATS = "workday"
_ENDPOINT = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_VIEW = "https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"

PAGE_LIMIT = 20


def parse(payload: Any, tenant: str, wd: str, site: str) -> list[Posting]:
    """Map one CXS page onto postings (pure — no network)."""
    if not isinstance(payload, dict):
        return []
    out: list[Posting] = []
    for job in payload.get("jobPostings") or []:
        if not isinstance(job, dict):
            continue
        path = str(job.get("externalPath") or "")
        title = str(job.get("title") or "")
        if not path or not title:
            continue
        remote_type = str(job.get("remoteType") or "").lower()
        out.append(
            Posting(
                title=title,
                company=tenant,
                url=_VIEW.format(tenant=tenant, wd=wd, site=site, path=path),
                ats=ATS,
                tenant=tenant,
                location=str(job.get("locationsText") or ""),
                description="",
                desc_available=False,
                req_id=path.rsplit("_", 1)[-1] if "_" in path else path,
                posted_at=None,
                remote=True if "remote" in remote_type else None,
            )
        )
    return out


def _paths(payload: Any) -> frozenset[str]:
    if not isinstance(payload, dict):
        return frozenset()
    return frozenset(
        str(j.get("externalPath") or "")
        for j in payload.get("jobPostings") or []
        if isinstance(j, dict) and j.get("externalPath")
    )


class WorkdaySource:
    """PostingSourcePort over one Workday tenant's career site."""

    name = ATS

    def __init__(
        self,
        tenant: str,
        wd: str,
        site: str,
        *,
        search_text: str = "software engineer",
        max_pages: int = 15,
    ) -> None:
        self._tenant = tenant
        self._wd = wd
        self._site = site
        self._search = search_text
        self._max_pages = max(1, max_pages)

    def fetch(self) -> list[Posting]:
        url = _ENDPOINT.format(tenant=self._tenant, wd=self._wd, site=self._site)
        collected: list[Posting] = []
        seen_pages: set[frozenset[str]] = set()
        for page in range(self._max_pages):
            body = {
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": page * PAGE_LIMIT,
                "searchText": self._search,
            }
            payload = post_json(url, body)
            paths = _paths(payload)
            if not paths or paths in seen_pages:
                break  # empty page, or the offset wrapped back onto a page we have
            seen_pages.add(paths)
            collected.extend(parse(payload, self._tenant, self._wd, self._site))
        return collected
