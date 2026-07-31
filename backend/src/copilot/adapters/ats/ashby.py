"""Ashby job-board adapter.

Public posting API, one call per board, and it skews modern tech startups — the
target segment. ``descriptionPlain`` means no HTML handling is needed, though we
fall back to ``descriptionHtml`` because a few boards populate only that.

``isListed: false`` postings are drafts or internal; they are skipped.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.ats._dates import parse_iso
from copilot.adapters.ats._http import get_json
from copilot.adapters.ats._text import html_to_text
from copilot.domain.posting import Posting

ATS = "ashby"
_BASE = "https://api.ashbyhq.com/posting-api/job-board/{tenant}"


def parse(payload: Any, tenant: str) -> list[Posting]:
    """Map an Ashby board payload onto postings (pure — no network)."""
    if not isinstance(payload, dict):
        return []
    out: list[Posting] = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        if job.get("isListed") is False:
            continue
        url = str(job.get("jobUrl") or job.get("applyUrl") or "")
        title = str(job.get("title") or "")
        if not url or not title:
            continue
        description = str(job.get("descriptionPlain") or "") or html_to_text(
            job.get("descriptionHtml")
        )
        remote = job.get("isRemote")
        out.append(
            Posting(
                title=title,
                company=tenant,
                url=url,
                ats=ATS,
                tenant=tenant,
                location=str(job.get("location") or ""),
                description=description,
                desc_available=bool(description),
                req_id=str(job.get("id") or ""),
                posted_at=parse_iso(job.get("publishedAt")),
                remote=bool(remote) if isinstance(remote, bool) else None,
                employment_type=str(job.get("employmentType") or ""),
            )
        )
    return out


class AshbySource:
    """PostingSourcePort over one Ashby board."""

    name = ATS

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant

    def fetch(self) -> list[Posting]:
        return parse(get_json(_BASE.format(tenant=self._tenant)), self._tenant)
