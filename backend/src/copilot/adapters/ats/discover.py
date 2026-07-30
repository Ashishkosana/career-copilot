"""Watchlist discovery — turn apply URLs into ``(company, ats, tenant)`` triples.

No ATS API enumerates its own tenants, so *which* boards to poll has to come from
somewhere else. An apply URL is the cheapest possible source: its hostname names
the ATS and its path names the tenant. Two feeds produce them in bulk —
Simplify's public ``listings.json``, and site-restricted searches such as
``site:myworkdayjobs.com "junior software developer"`` (a slug factory, run by
hand and occasionally, not in a loop).

Hosts we can classify but not fetch (SmartRecruiters, iCIMS, Oracle, Eightfold)
are reported separately rather than dropped silently, so the coverage gap stays
visible instead of looking like an absence of jobs.
"""
from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from copilot.adapters.ats.watchlist import SUPPORTED_ATS, WatchlistEntry

# <tenant>.wd<N>.myworkdayjobs.com
_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.I)
# Locale segments Workday sometimes injects ahead of the career-site slug.
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")

# Greenhouse route segments that are not company slugs.
_GREENHOUSE_NON_TENANT_PATHS = frozenset({"embed", "job_board", "jobs"})

_SIMPLE_HOSTS: dict[str, str] = {
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
    "jobs.lever.co": "lever",
    "apply.workable.com": "workable",
    # Classifiable, not fetchable by this package — see module docstring.
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.icims.com": "icims",
}


@dataclass
class DiscoveryReport:
    """Coverage accounting for one discovery pass."""

    total_urls: int = 0
    classified: int = 0
    supported: int = 0
    unsupported_ats: Counter[str] = field(default_factory=Counter)
    unclassified_hosts: Counter[str] = field(default_factory=Counter)

    @property
    def coverage(self) -> float:
        return self.classified / self.total_urls if self.total_urls else 0.0


def classify_apply_url(url: str, company: str = "") -> WatchlistEntry | None:
    """Map an apply URL onto a watchlist entry, or ``None`` if unrecognised.

    Returns an entry even for ATSs this package cannot fetch; callers filter on
    ``entry.ats in SUPPORTED_ATS`` so the gap is explicit at the call site.
    """
    if not url:
        return None
    parsed = urllib.parse.urlparse(url if "//" in url else f"https://{url}")
    host = parsed.netloc.lower().split(":")[0]
    segments = [s for s in parsed.path.split("/") if s]

    workday = _WORKDAY_HOST.match(host)
    if workday:
        return _workday_entry(workday, segments, company)

    ats = _SIMPLE_HOSTS.get(host)
    if not ats:
        return None

    tenant = segments[0] if segments else ""
    if ats == "greenhouse" and tenant in _GREENHOUSE_NON_TENANT_PATHS:
        # Embedded boards look like boards.greenhouse.io/embed/job_board?for=<tenant>
        # — the first path segment is a route, not a company.
        query = urllib.parse.parse_qs(parsed.query)
        tenant = (query.get("for") or [""])[0]
    if not tenant:
        return None
    try:
        return WatchlistEntry(company=company or tenant, ats=ats, tenant=tenant)
    except ValueError:
        # Recognised host, ATS we do not model — surface it as unsupported.
        return None


def _workday_entry(
    match: re.Match[str], segments: list[str], company: str
) -> WatchlistEntry | None:
    """Workday encodes the tenant in the host and the career site in the path."""
    site = next((s for s in segments if not _LOCALE.match(s)), "")
    if not site:
        return None
    tenant = match.group("tenant")
    return WatchlistEntry(
        company=company or tenant,
        ats="workday",
        tenant=tenant,
        wd=match.group("wd").lower(),
        site=site,
    )


def _is_supported(entry: WatchlistEntry | None) -> bool:
    return entry is not None and entry.ats in SUPPORTED_ATS


def watchlist_from_apply_urls(
    rows: Iterable[tuple[str, str]],
) -> tuple[list[WatchlistEntry], DiscoveryReport]:
    """Build a deduplicated watchlist from ``(company, apply_url)`` pairs."""
    report = DiscoveryReport()
    by_key: dict[tuple[str, str, str, str], WatchlistEntry] = {}
    for company, url in rows:
        report.total_urls += 1
        entry = classify_apply_url(url, company)
        if entry is None:
            host = urllib.parse.urlparse(url if "//" in url else f"https://{url}").netloc.lower()
            if host in _SIMPLE_HOSTS:
                report.unsupported_ats[_SIMPLE_HOSTS[host]] += 1
                report.classified += 1
            elif host:
                report.unclassified_hosts[host] += 1
            continue
        report.classified += 1
        if entry.ats not in SUPPORTED_ATS:
            report.unsupported_ats[entry.ats] += 1
            continue
        report.supported += 1
        by_key.setdefault((entry.ats, entry.tenant, entry.wd, entry.site), entry)
    return sorted(by_key.values(), key=lambda e: (e.ats, e.tenant)), report


def watchlist_from_simplify(
    listings: Any, *, active_only: bool = True
) -> tuple[list[WatchlistEntry], DiscoveryReport]:
    """Build a watchlist from Simplify's ``listings.json`` (pure — no network)."""
    if not isinstance(listings, list):
        return [], DiscoveryReport()
    rows: list[tuple[str, str]] = []
    for row in listings:
        if not isinstance(row, dict):
            continue
        if active_only and not row.get("active"):
            continue
        rows.append((str(row.get("company_name") or ""), str(row.get("url") or "")))
    return watchlist_from_apply_urls(rows)


def sponsorship_hints(listings: Any) -> dict[str, str]:
    """Company → Simplify's declared sponsorship stance, lowercased.

    Simplify publishes a ``sponsorship`` field per listing. It is *third-party
    metadata about an employer*, not a statement in the posting itself, so it
    belongs in a hint table a human reads — never as an automatic hard gate.
    """
    hints: dict[str, str] = {}
    if not isinstance(listings, list):
        return hints
    for row in listings:
        if not isinstance(row, dict):
            continue
        company = str(row.get("company_name") or "").strip().lower()
        stance = str(row.get("sponsorship") or "").strip().lower()
        if company and stance:
            hints.setdefault(company, stance)
    return hints
