"""Two gates that exist because the published page was already wrong.

Both defects were found by reading the *output* — the 880 roles actually baked into
``docs/index.html`` — not by reading the code. Neither the agents that built the
pipeline nor the agent that verified it caught either one, which is the argument for
asserting them here: a suite that only tests what the code intends cannot catch the
code doing exactly what it intends to the wrong data.

1. **19 postings came from ATS vendor demo boards.** ``leverdemo`` is Lever's public
   sandbox. Its roles are structurally perfect — real ATS payloads, real fields, and
   they pass every gate — but the companies are invented and one is dated 2013. The
   project exists because the previous version served four invented companies as
   real matches; publishing a vendor's fixtures is that same failure with better
   plumbing.

2. **50 internships survived, and 5 of them ranked *exact match*.** An internship is
   not a junior full-time role, it is a different product, and it cannot support the
   work authorisation this search exists to obtain. Five of the twenty-five slots in
   the one list whose job is to be short and correct went to roles that could never
   be accepted.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from copilot.adapters.ats.watchlist import parse_watchlist
from copilot.domain.demo_boards import is_demo_tenant
from copilot.domain.posting import Posting
from copilot.domain.screening import Exclusion, is_internship, screen, screen_all
from copilot.domain.seniority import JUNIOR_BANDS, Level

FIXED_NOW = datetime(2026, 7, 6, 14, 0, 0, tzinfo=UTC)


def posting(
    title: str, *, employment_type: str = "", desc: str = "", company: str = "Acme"
) -> Posting:
    return Posting(
        title=title,
        company=company,
        url=f"https://boards.example/{abs(hash(title)) % 10**8}",
        ats="greenhouse",
        location="Remote (US)",
        description=desc or "Build REST APIs in Python on AWS. 1+ years of experience.",
        desc_available=True,
        employment_type=employment_type,
        posted_at=FIXED_NOW,
    )


class TestDemoTenantGate:
    """The two boards that were live, and the legitimate names next to them."""

    @pytest.mark.parametrize(
        "tenant",
        [
            "leverdemo",  # was on the watchlist — Lever's own sandbox
            "salesdemo-jr",  # was on the watchlist
            "sandbox-co",
            "acme_sandbox_2",
            "staging-jr",
            "test",
            "example",
        ],
    )
    def test_demo_boards_are_rejected(self, tenant: str) -> None:
        assert is_demo_tenant(tenant)

    @pytest.mark.parametrize(
        "tenant",
        [
            "demodesk",  # a real company; "demo" is a prefix of its name
            "democracy",
            "demoulas",
            "testing",
            "9fin",  # a real watchlist entry, and it starts with a digit
            "acme",
        ],
    )
    def test_real_companies_whose_names_contain_a_marker_survive(self, tenant: str) -> None:
        """The word-boundary half. Dropping a real board is also a defect."""
        assert not is_demo_tenant(tenant)

    def test_the_gate_is_in_the_parser_not_the_json_file(self) -> None:
        """``discover.py`` rewrites watchlist.json, so a hand-edit would not hold."""
        payload = {
            "companies": [
                {"company": "Lever", "ats": "lever", "tenant": "leverdemo"},
                {"company": "9fin", "ats": "ashby", "tenant": "9fin"},
            ]
        }
        entries = parse_watchlist(payload)
        assert [e.tenant for e in entries] == ["9fin"]

    def test_a_test_can_still_opt_into_a_demo_board_deliberately(self) -> None:
        payload = {"companies": [{"company": "Lever", "ats": "lever", "tenant": "leverdemo"}]}
        assert parse_watchlist(payload, allow_demo=True)[0].tenant == "leverdemo"

    def test_the_live_watchlist_has_no_demo_board_left(self) -> None:
        path = Path(__file__).resolve().parents[2] / "data" / "watchlist.json"
        if not path.exists():  # pragma: no cover - the corpus is gitignored
            pytest.skip("data/watchlist.json is generated; see scripts/discover.py")
        raw = json.loads(path.read_text())
        entries = parse_watchlist(raw)
        assert entries, "the watchlist parsed to nothing — the gate is too broad"
        assert not [e for e in entries if is_demo_tenant(e.tenant)]
        # And the guard really removed something, or this test proves nothing.
        allowed = parse_watchlist(raw, allow_demo=True)
        assert len(allowed) > len(entries), "no demo board was filtered; is the fixture stale?"


class TestInternshipGate:
    """Real titles from the corpus, and the near-misses that must survive."""

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer Intern (Fall 2026)",
            "Software Engineering Intern",
            "Quantitative Developer Intern (Python)",
            "2027 Internship - Software Engineer",
            "Summer Intern 2027 - Software Developer",
            "Software Engineer (Agent Platform) - Intern - 2026-2027",
            "Forward Deployed Software Engineer, Internship - USG",
            "Software Engineer, Intern",
            "Co-op Software Developer",
            "Software Developer Coop",
            "Apprentice Software Engineer",
            "Working Student Software Engineer",
        ],
    )
    def test_internship_titles_are_excluded(self, title: str) -> None:
        decision = screen(posting(title))
        assert Exclusion.INTERNSHIP in decision.exclusions
        assert not decision.kept

    @pytest.mark.parametrize(
        "title",
        [
            # Every one of these is the missing-word-boundary bug waiting to happen.
            # Unanchored `intern` matches all three.
            "Software Engineer I - Internal Tooling",
            "Internal Tools Engineer",
            "Internationalization Engineer",
            "Internals Engineer",
            "Software Engineer, Internal Platform",
            # `co-?ops?` unanchored would take these.
            "Cooperative Systems Engineer",
            "Software Engineer, Coordination",
            # A genuine junior full-time role must be untouched.
            "New Grad Software Engineer",
            "Software Engineer I",
            "Junior Backend Engineer",
        ],
    )
    def test_titles_that_merely_contain_the_letters_survive(self, title: str) -> None:
        decision = screen(posting(title))
        assert Exclusion.INTERNSHIP not in decision.exclusions, (
            f"{title!r} was gated as an internship — a word boundary is missing"
        )

    def test_employment_type_catches_a_clean_title(self) -> None:
        """22 postings declare the type and hide it from the title entirely."""
        assert is_internship("Software Engineer, Product", "Intern")
        assert is_internship("Software Engineer", "Internship")
        decision = screen(posting("Software Engineer, Product", employment_type="Intern"))
        assert Exclusion.INTERNSHIP in decision.exclusions

    def test_full_time_employment_types_are_not_gated(self) -> None:
        """Free text across five boards — these are all real values in the corpus."""
        for kind in (
            "FullTime",
            "Full Time",
            "Full-time",
            "Full-Time/ Salary",
            "Permanent",
            "Permanent contract & B2B",
            "Temporary FT",
            "Regular Full Time (Salary)",
            "Contract",
            "",
        ):
            assert not is_internship("Software Engineer", kind), kind

    def test_the_reason_quotes_the_word_that_triggered_it(self) -> None:
        """An excluded posting is browsable, so its reason has to be checkable."""
        decision = screen(posting("Software Engineer Intern (Fall 2026)"))
        reason = decision.reason_for(Exclusion.INTERNSHIP)
        assert "Intern" in reason
        assert "not full-time" in reason

    def test_the_reason_falls_back_to_the_type_when_the_title_is_clean(self) -> None:
        decision = screen(posting("Software Engineer, Product", employment_type="Internship"))
        assert "Internship" in decision.reason_for(Exclusion.INTERNSHIP)

    def test_an_internship_is_gated_even_when_it_would_have_scored_perfectly(self) -> None:
        """The actual defect: 5 internships reached the exact-match list.

        A posting that satisfies every other gate is exactly the one this gate has to
        catch, because it is the one that would otherwise rank at the top.
        """
        decision = screen(
            posting(
                "Software Engineer Intern (Fall 2026)",
                employment_type="Intern",
                desc=(
                    "Python, REST APIs, AWS Lambda, DynamoDB, React, TypeScript, pytest, "
                    "CI/CD. 0-2 years of experience. We sponsor visas."
                ),
            )
        )
        assert decision.exclusions == (Exclusion.INTERNSHIP,), (
            "it failed only this gate — which is why it outranked real roles before"
        )


class TestStoredDemoPostingsCannotReachThePage:
    """The half a fetch-time gate cannot reach.

    317 demo postings were already in ``data/postings.db`` when the watchlist guard
    went in — 296 from ``leverdemo`` and 21 from ``salesdemo-jr`` — and neither store
    exposes a delete. So the gate has to run at read time too, or the guard fixes
    only tomorrow's page and leaves today's wrong.
    """

    def test_a_stored_demo_posting_is_excluded_at_screen_time(self) -> None:
        decision = screen(posting("Software Engineer", company="leverdemo"))
        assert Exclusion.DEMO_BOARD in decision.exclusions
        assert not decision.kept

    def test_the_reason_names_the_board(self) -> None:
        decision = screen(posting("Backend Engineer", company="salesdemo-jr"))
        reason = decision.reason_for(Exclusion.DEMO_BOARD)
        assert "salesdemo-jr" in reason
        assert "not an employer" in reason

    def test_a_real_company_is_untouched(self) -> None:
        for company in ("Commure", "9fin", "Figma", "Demodesk", "Notion"):
            decision = screen(posting("Junior Software Engineer", company=company))
            assert Exclusion.DEMO_BOARD not in decision.exclusions, company

    def test_the_gate_is_pure_domain_and_imports_no_adapter(self) -> None:
        """The first version had ``domain`` importing ``adapters`` — the one banned edge."""
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "copilot" / "domain" / "demo_boards.py"
        ).read_text()
        assert "copilot.adapters" not in source
        assert "import re" in source


class TestInternshipsAreAProductChoiceNotAFact:
    def test_the_flag_puts_them_back(self) -> None:
        """A searcher who wants internships must not have to edit the seniority model."""
        intern = posting("Software Engineer Intern (Fall 2026)")
        assert Exclusion.INTERNSHIP in screen(intern).exclusions
        assert Exclusion.INTERNSHIP not in screen(intern, include_internships=True).exclusions

    def test_intern_stays_a_recognised_band(self) -> None:
        """Level classification is a fact about the posting, not a preference."""
        assert Level.INTERN in JUNIOR_BANDS, (
            "the internship *gate* owns this decision; the band model must stay factual"
        )

    def test_screen_all_threads_the_flag(self) -> None:
        rows = [posting("Software Engineer Intern"), posting("Junior Backend Engineer")]
        kept_default, _, _ = screen_all(rows)
        kept_with, _, _ = screen_all(rows, include_internships=True)
        assert len(kept_default) == 1
        assert len(kept_with) == 2
