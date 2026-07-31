"""Seed a throwaway posting store, so the page generator can be run with no corpus.

``build_ui.py``'s primary path drives the real read handlers against a real store, and
that is the only check in this repo that exercises the whole chain the published page
depends on: cron-materialised screening view → read API → public projection → rendered
HTML. It was not under CI, and that is exactly how a read API that answered 503 (and,
before that, 504) on every route reached production with every other gate green.

CI has no corpus — ``data/postings.db`` is gitignored and is 150 MB — so this writes a
handful of postings chosen to make the page's own invariants non-trivial: enough kept
roles to page, one software internship (so the internships collection is not empty), one
posting that fails several gates at once (so ``/excluded`` has a row under more than one
gate), and one with no description at all (so ``descriptionStatus`` has to distinguish
"none offered" from "none published").

It writes the corpus only. ``build_ui.py`` publishes the screening view itself, through
the production builder — which is the point: a seeder that also hand-wrote the view
would prove the page agrees with a shape this file invented.

    python tools/ui/seed_demo_store.py /tmp/demo.db
    COPILOT_POSTINGS_DB_PATH=/tmp/demo.db python tools/ui/build_ui.py --check-js
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from copilot.adapters.sqlite_posting_store import (  # noqa: E402
    SqlitePostingStore,
)
from copilot.domain.posting import Posting  # noqa: E402

NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)

#: ``prog`` plus the destination path.
_ARGC = 2

_JUNIOR = (
    "We are hiring an entry-level engineer. You will work in Python and TypeScript on "
    "AWS. 0-2 years of experience. New grads welcome. Docker and PostgreSQL a plus."
)


def _posting(
    n: int,
    title: str,
    *,
    description: str = _JUNIOR,
    desc_available: bool = True,
    company: str = "Example Corp",
    ats: str = "greenhouse",
    tenant: str = "examplecorp",
    days_ago: int = 1,
) -> Posting:
    return Posting(
        title=title,
        company=f"{company} {n}",
        url=f"https://boards.example.invalid/{tenant}/jobs/{n}",
        ats=ats,
        tenant=tenant,
        location="Tempe, AZ",
        description=description,
        desc_available=desc_available,
        req_id=f"REQ-{n}",
        posted_at=NOW - timedelta(days=days_ago),
        remote=n % 2 == 0,
        employment_type="Intern" if "Intern" in title else "Full-time",
    )


def demo_postings() -> list[Posting]:
    """A corpus small enough to screen instantly and varied enough to be a real test."""
    postings = [
        # Kept: several, so /worklist has more than one page at limit=1 and the keyset
        # cursor is actually exercised rather than trivially None.
        *(
            _posting(n, "Software Engineer I", days_ago=n)
            for n in range(1, 6)
        ),
        _posting(10, "New Grad Software Engineer", days_ago=2),
        # Undated: must sort last and must not be dropped.
        Posting(
            title="Junior Software Engineer",
            company="Undated Corp",
            url="https://boards.example.invalid/examplecorp/jobs/11",
            ats="lever",
            tenant="examplecorp",
            description=_JUNIOR,
        ),
        # One software internship: the internships collection must not be empty, or the
        # reconciliation check between the gate count and the collection proves nothing.
        _posting(20, "Software Engineering Intern", days_ago=3),
        # A non-software internship: hits the internship gate *and* the not-a-software-
        # role gate, so a posting is filed under two views and /excluded has to page per
        # gate rather than per posting.
        _posting(21, "Marketing Intern", days_ago=4),
        # Senior: the seniority gate, with the evidence quoted from the description.
        _posting(
            30,
            "Staff Software Engineer",
            description="10+ years of experience required. You will lead a team.",
            days_ago=5,
        ),
        # Clearance and citizenship, in one posting.
        _posting(
            31,
            "Software Engineer",
            description=(
                "Requires an active TS/SCI security clearance. Must be a U.S. citizen. "
                "Python and AWS."
            ),
            days_ago=6,
        ),
        # No sponsorship.
        _posting(
            32,
            "Software Engineer",
            description=(
                "Entry level, Python and AWS. This employer will not sponsor or "
                "transfer visas for this position."
            ),
            days_ago=7,
        ),
        # No description at all — Workday's list endpoint. `descriptionStatus` has to
        # say "not provided by the source", and the eligibility gates could not run.
        _posting(
            40,
            "Software Engineer, Platform",
            description="",
            desc_available=False,
            ats="workday",
            days_ago=8,
        ),
        # An ATS vendor demo board: must appear under its gate and in no collection.
        _posting(50, "Software Engineer I", ats="lever", tenant="leverdemo", days_ago=9),
        # Not a software role.
        _posting(60, "Registered Nurse", days_ago=10),
    ]
    return postings


def main(argv: list[str]) -> int:
    if len(argv) != _ARGC:
        print(f"usage: {Path(argv[0]).name} <path/to/demo.db>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    store = SqlitePostingStore(path)
    postings = demo_postings()
    new, known = store.sync(postings, now=NOW)
    store.close()
    print(f"seeded {path}: {len(new)} new, {len(known)} already known")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
