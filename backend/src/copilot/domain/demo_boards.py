"""Whether a job-board tenant is an ATS vendor's demo sandbox rather than an employer.

Pure and dependency-free, and in ``domain/`` rather than beside the watchlist adapter
that first needed it, because **two** layers have to ask the question and they sit on
opposite sides of the hexagon:

* ``adapters/ats/watchlist.py`` filters demo boards out of the next fetch.
* ``domain/screening.py`` filters the 317 that are already stored, which no fetch-time
  gate can reach and no store exposes a delete for.

Putting it here keeps the dependency arrow pointing inward. The first version of this
gate lived in the adapter and ``screening.py`` imported it, which had the domain
depending on an adapter — the one rule the whole layout exists to enforce.
"""
from __future__ import annotations

import re

#: Tenant slugs that serve an ATS vendor's own demo fixtures rather than a real
#: employer's openings. ``leverdemo`` is Lever's public sandbox; its board answers
#: with invented roles dated as far back as 2013 and they screen exactly like real
#: ones, because structurally they *are* real postings — just not real jobs.
#:
#: This gate lives in the domain rather than in the JSON file on purpose:
#: ``discover.py`` regenerates ``watchlist.json``, so deleting two lines by hand
#: would put them back on the next sweep. Publishing a vendor's demo fixtures on a
#: page whose entire origin story is "the old version served invented companies as
#: real matches" is the same failure wearing a different hat.
_DEMO_MARKERS = "demo|sandbox|staging|playground|dummy"
_DEMO_TENANT = re.compile(
    # delimited anywhere: "salesdemo-jr", "acme_sandbox_2", "staging-jr"
    rf"(?:^|[-_])(?:{_DEMO_MARKERS})(?:[-_]|$)"
    # or fused as a suffix, which is how vendors name their own board: "leverdemo"
    rf"|(?:{_DEMO_MARKERS})$"
    # or the whole slug is a placeholder
    r"|^(?:test|example|foo|bar)$",
    re.I,
)

#: The two on the live watchlist, kept explicitly. The pattern above covers the
#: class, this covers the instances — a pattern I have already got wrong once should
#: not be the only thing standing between a vendor's fixtures and a public page.
_KNOWN_DEMO_TENANTS = frozenset({"leverdemo", "salesdemo-jr"})


def is_demo_tenant(tenant: str) -> bool:
    """Whether a tenant slug belongs to a vendor demo board.

    Pattern-matched at slug boundaries rather than by bare substring, because
    ``demo`` sits inside legitimate names: "demodesk" and "democracy" are plausible
    tenants and are left alone. A *fused suffix* still counts, since that is how
    vendors name their own sandboxes — hence ``leverdemo`` matches.

    The residual false-positive is a real company whose slug ends in a marker
    ("modemo"). That trade is deliberate and one-sided: being wrong here drops 1
    board out of 819, and being wrong the other way publishes invented jobs on a
    page whose credibility is the entire point.
    """
    slug = tenant.strip().lower()
    return slug in _KNOWN_DEMO_TENANTS or bool(_DEMO_TENANT.search(slug))
