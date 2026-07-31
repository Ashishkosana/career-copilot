"""A raw job posting as fetched from an ATS, before any gating or scoring.

``Posting`` is deliberately *pre-judgement*: it carries what the source actually
returned and nothing derived. The one non-obvious field is
:attr:`Posting.desc_available` — several sources (Workday's list endpoint, and a
handful of Lever records) return no description at all. Without an explicit flag
those rows silently *pass* every description-based gate, because ``"" `` never
matches an exclusion pattern. Callers must route ``desc_available is False`` rows
to title-only gates instead of treating them as clean.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field


class Posting(BaseModel):
    """One open role from an ATS job board."""

    model_config = {"frozen": True}

    title: str
    company: str
    url: str
    ats: str
    tenant: str = ""
    location: str = ""
    description: str = ""
    desc_available: bool = True
    req_id: str = ""
    posted_at: datetime | None = None
    remote: bool | None = None
    employment_type: str = ""
    experience_level: str = Field(
        default="",
        description="Vendor-declared seniority. Only SmartRecruiters populates this.",
    )

    @property
    def id(self) -> str:
        """Stable id keyed on the posting URL (matches ``scoring.job_id``)."""
        return hashlib.sha1(self.url.encode()).hexdigest()[:16]

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """Companies re-post identical requisitions; collapse on this."""
        return (self.company.strip().lower(), self.title.strip().lower())
