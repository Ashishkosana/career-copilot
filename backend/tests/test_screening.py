"""Seniority, eligibility and the screening funnel.

Almost every case here is a real string from a live posting that broke an earlier
version of this logic. The inversion test at the bottom is the phase gate: it
asserts the specific pair the shipped scorer got backwards.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot.domain.eligibility import Sponsorship, screen_eligibility
from copilot.domain.posting import Posting
from copilot.domain.screening import Exclusion, is_software_role, screen, screen_all
from copilot.domain.seniority import (
    JUNIOR_BANDS,
    Level,
    LevelSource,
    classify_level,
    decide_level,
    extract_min_years,
    resolve_level,
)


def make(title: str, *, desc: str = "", has_desc: bool | None = None, when: int = 1) -> Posting:
    return Posting(
        title=title,
        company="Acme",
        url=f"https://x/{abs(hash(title)) % 10**8}/{when}",
        ats="greenhouse",
        description=desc,
        desc_available=bool(desc) if has_desc is None else has_desc,
        posted_at=datetime(2026, 7, when, tzinfo=UTC),
    )


class TestClassifyLevel:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("New Grad Software Engineer", Level.ENTRY),
            ("Software Engineer, New College Grad", Level.ENTRY),
            ("Associate Software Engineer", Level.ENTRY),
            ("Junior Developer", Level.ENTRY),
            ("Jr. Backend Engineer", Level.ENTRY),
            ("Software Engineer I", Level.ENTRY),
            ("SWE 1", Level.ENTRY),
            ("Software Engineer, Early Career", Level.ENTRY),
            ("Software Engineer Intern", Level.INTERN),
            ("Software Engineering Co-op", Level.INTERN),
            ("Software Engineer II", Level.MID),
            ("Software Engineer III", Level.MID),
            ("SDE 3", Level.MID),
            ("Senior Staff Engineer", Level.SENIOR),
            ("Staff Software Engineer", Level.SENIOR),
            ("Principal Engineer", Level.SENIOR),
            ("Engineering Manager", Level.SENIOR),
            ("Head of Platform", Level.SENIOR),
            ("Distinguished Engineer", Level.SENIOR),
            ("Software Architect", Level.SENIOR),
            ("Software Engineer", Level.UNKNOWN),
        ],
    )
    def test_titles(self, title: str, expected: Level) -> None:
        assert classify_level(title) == expected

    def test_senior_marker_beats_level_numeral(self) -> None:
        """Samsara really posts this; 'Engineer I' inside a Senior title is senior."""
        assert classify_level("Senior Software Engineer I - Agent Foundations") == Level.SENIOR
        assert classify_level("Senior Software Engineer I — Agentic Analytics") == Level.SENIOR

    def test_international_is_not_an_internship(self) -> None:
        """`intern(?!al)` matched 'Identity International' on four real Affirm rows."""
        assert classify_level("Senior Software Engineer, Backend (Identity International)") == (
            Level.SENIOR
        )
        assert classify_level("Software Engineer, International") == Level.UNKNOWN

    def test_leadership_is_not_lead(self) -> None:
        assert classify_level("Software Engineer, Leadership Tools") == Level.UNKNOWN

    def test_engineer_i_does_not_match_ii(self) -> None:
        assert classify_level("Engineer II") == Level.MID
        assert classify_level("Engineer I") == Level.ENTRY


class TestYearsOfExperience:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3+ years of experience required", 3),
            ("5 years of relevant experience", 5),
            ("2 to 4 years of professional experience", 2),
            ("3-5 years' experience building systems", 3),
            ("Minimum of 7 years", 7),
            ("At least 1 year of hands-on experience", 1),
            ("2+ years required, 5+ preferred", 2),
            ("No numbers here at all", None),
            ("", None),
        ],
    )
    def test_extraction(self, text: str, expected: int | None) -> None:
        assert extract_min_years(text) == expected

    def test_ignores_education_phrases(self) -> None:
        assert extract_min_years("4-year degree required") is None
        assert extract_min_years("Graduating in 2026 with a BS in CS") is None
        assert extract_min_years("Completing a 4 year program") is None

    def test_takes_the_minimum_not_the_first(self) -> None:
        assert extract_min_years("8 years preferred; at least 2 years required") == 2


class TestResolveLevel:
    def test_years_can_push_an_unmarked_title_up(self) -> None:
        """504 of 1,127 real SWE titles carry no level marker; years disambiguate."""
        assert resolve_level("Software Engineer", "5+ years of experience") == Level.MID
        assert resolve_level("Software Engineer", "1+ years of experience") == Level.ENTRY

    def test_years_never_pull_a_senior_title_down(self) -> None:
        assert resolve_level("Senior Software Engineer", "2+ years of experience") == Level.SENIOR

    def test_missing_description_falls_back_to_title(self) -> None:
        """Workday's list endpoint has no description — title-only, not 'clean'."""
        assert resolve_level("Software Engineer", "", desc_available=False) == Level.UNKNOWN
        assert resolve_level("Senior Engineer", "", desc_available=False) == Level.SENIOR


class TestEligibility:
    def test_clearance(self) -> None:
        result = screen_eligibility("Must hold an active TS/SCI clearance with polygraph.")
        assert result.clearance_required is True
        assert result.excluded is True
        assert "clearance" in dict(result.evidence)

    def test_citizenship_and_itar(self) -> None:
        assert screen_eligibility("Applicants must be a US citizen.").citizenship_required
        assert screen_eligibility("This role is subject to ITAR.").citizenship_required
        assert screen_eligibility("U.S. citizens only.").citizenship_required

    def test_negative_sponsorship(self) -> None:
        for text in [
            "We are unable to provide visa sponsorship for this role.",
            "This position does not offer sponsorship.",
            "No visa sponsorship is available.",
            "Candidates must be authorized to work without sponsorship.",
        ]:
            assert screen_eligibility(text).sponsorship is Sponsorship.WILL_NOT_SPONSOR, text

    def test_positive_sponsorship(self) -> None:
        result = screen_eligibility("We will provide visa sponsorship for the right candidate.")
        assert result.sponsorship is Sponsorship.WILL_SPONSOR
        assert result.excluded is False

    def test_silence_is_unstated_not_a_refusal(self) -> None:
        """Most postings say nothing; treating silence as refusal discards the market."""
        result = screen_eligibility("Build great software with a great team.")
        assert result.sponsorship is Sponsorship.UNSTATED
        assert result.excluded is False

    def test_relocation_refusal_is_not_a_sponsorship_refusal(self) -> None:
        result = screen_eligibility("We are unable to provide relocation assistance.")
        assert result.sponsorship is Sponsorship.UNSTATED

    def test_no_description_is_unchecked_not_eligible(self) -> None:
        """The Workday trap: an empty string matches no exclusion pattern."""
        result = screen_eligibility("", desc_available=False)
        assert result.checked is False
        assert result.excluded is False


class TestRoleFamily:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer",
            "Backend Engineer",
            "Full Stack Developer",
            "Mobile Engineer, iOS",
            "SWE I",
            "Platform Engineer",
        ],
    )
    def test_accepts_software_roles(self, title: str) -> None:
        assert is_software_role(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "AI Solutions Engineer",
            "Sales Engineer",
            "Associate Sales Engineer, UK&I",
            "Customer Success Engineer",
            "Data Scientist",
            "Product Manager",
            "Technical Writer",
            "Mechanical Engineer",
            "Analytics Engineer",
        ],
    )
    def test_rejects_non_software_roles(self, title: str) -> None:
        """'AI Solutions Engineer' was surfaced twice in the old scorer's top 8."""
        assert is_software_role(title) is False


class TestScreen:
    def test_keeps_a_clean_new_grad_role(self) -> None:
        decision = screen(make("New Grad Software Engineer", desc="Build things in Python."))
        assert decision.kept is True
        assert decision.level is Level.ENTRY

    def test_excludes_senior_by_band(self) -> None:
        decision = screen(make("Senior Staff Engineer", desc="Python, AWS, Kubernetes, Kafka."))
        assert decision.kept is False
        assert Exclusion.LEVEL in decision.exclusions

    def test_excludes_clearance_and_quotes_the_phrase(self) -> None:
        decision = screen(
            make("Software Engineer I", desc="Requires an active Top Secret clearance.")
        )
        assert Exclusion.CLEARANCE in decision.exclusions
        assert any("clearance required" in r for r in decision.reasons)

    def test_unknown_level_survives_for_the_llm_tier(self) -> None:
        """45% of SWE titles have no marker; dropping them discards the market."""
        decision = screen(make("Software Engineer", desc="Join our team building products."))
        assert decision.kept is True
        assert decision.level is Level.UNKNOWN

    def test_multiple_exclusions_are_all_recorded(self) -> None:
        decision = screen(
            make("Principal Engineer", desc="US citizens only. Active clearance required.")
        )
        assert set(decision.exclusions) >= {
            Exclusion.LEVEL,
            Exclusion.CITIZENSHIP,
            Exclusion.CLEARANCE,
        }


class TestWorklist:
    def test_sorted_newest_first(self) -> None:
        postings = [
            make("Software Engineer I", desc="d", when=1),
            make("New Grad Software Engineer", desc="d", when=20),
            make("Junior Developer", desc="d", when=10),
        ]
        kept, _, _ = screen_all(postings)
        assert [d.posting.posted_at.day for d in kept if d.posting.posted_at] == [20, 10, 1]

    def test_undated_sorts_last_but_is_not_dropped(self) -> None:
        dated = make("Software Engineer I", desc="d", when=5)
        undated = Posting(
            title="Junior Developer", company="B", url="https://x/u", ats="workday",
            desc_available=False,
        )
        kept, _, _ = screen_all([undated, dated])
        assert len(kept) == 2
        assert kept[0].posting.posted_at is not None

    def test_report_accounts_for_every_posting(self) -> None:
        postings = [
            make("New Grad Software Engineer", desc="d"),
            make("Senior Software Engineer", desc="d"),
            make("Sales Engineer", desc="d"),
            make("Software Engineer", desc="Requires TS/SCI clearance."),
        ]
        kept, excluded, report = screen_all(postings)
        assert report.total == 4
        assert report.kept == len(kept)
        assert len(kept) + len(excluded) == 4
        assert report.by_exclusion["wrong_seniority_band"] == 1
        assert report.by_exclusion["not_a_software_role"] == 1
        assert report.by_exclusion["security_clearance_required"] == 1


class TestInversionGate:
    """The phase gate: seniority must be categorical, not a score."""

    def test_the_exact_pair_the_old_scorer_got_backwards(self) -> None:
        """Old scorer: New Grad 36% (rejected), Senior Staff 42% (accepted)."""
        new_grad = make("New Grad Software Engineer", desc="BS in CS expected 2026. Python.")
        senior = make(
            "Senior Staff Engineer",
            desc="Python, AWS Lambda, DynamoDB, serverless, microservices, Kubernetes, "
            "Terraform, Kafka, gRPC, Postgres, mentoring, 10+ years of experience.",
        )
        kept, excluded, _ = screen_all([senior, new_grad])
        assert [d.posting.title for d in kept] == ["New Grad Software Engineer"]
        assert [d.posting.title for d in excluded] == ["Senior Staff Engineer"]

    def test_inversion_rate_is_zero_over_labelled_pairs(self) -> None:
        """No senior posting may ever appear at or above a junior one."""
        juniors = [
            "New Grad Software Engineer",
            "Software Engineer I",
            "Associate Software Engineer",
            "Junior Backend Engineer",
            "Software Engineer, Early Career",
        ]
        seniors = [
            "Senior Software Engineer",
            "Staff Software Engineer",
            "Principal Software Engineer",
            "Senior Software Engineer I - Platform",
            "Engineering Manager, Backend",
            "Software Engineer III",
        ]
        verbose = (
            "Python, AWS, Kubernetes, Terraform, Kafka, gRPC, Postgres, React, "
            "TypeScript, Docker, CI/CD, microservices, mentoring, architecture."
        )
        inversions = 0
        pairs = 0
        for jr in juniors:
            for sr in seniors:
                pairs += 1
                kept, _, _ = screen_all([make(sr, desc=verbose), make(jr, desc="Python basics.")])
                titles = [d.posting.title for d in kept]
                if sr in titles:
                    inversions += 1
        assert pairs == 30
        assert inversions == 0, f"{inversions}/{pairs} inversions survived the gate"

    def test_wanted_band_is_configurable(self) -> None:
        mid = make("Software Engineer II", desc="3 years of experience")
        assert screen(mid).kept is False
        assert screen(mid, wanted=JUNIOR_BANDS | {Level.MID}).kept is True


class TestGateFalsePositives:
    """Real recall losses found by running the funnel over 25,294 live postings."""

    @pytest.mark.parametrize(
        "text",
        [
            "Unlimited PTO and a military-friendly workplace.",
            "Sanitary conditions are maintained throughout.",
            "A solitary focus on customer outcomes.",
        ],
    )
    def test_itar_does_not_match_inside_ordinary_words(self, text: str) -> None:
        """'military', 'sanitary' and 'solitary' all contain the letters i-t-a-r."""
        assert screen_eligibility(text).citizenship_required is False

    def test_real_itar_still_caught(self) -> None:
        result = screen_eligibility(
            "Subject to International Traffic in Arms Regulations (ITAR)."
        )
        assert result.citizenship_required is True

    def test_explicit_entry_marker_beats_a_preferred_years_bullet(self) -> None:
        """"Software Engineer I" asking 3 years in *preferred* quals is still entry."""
        assert resolve_level("Software Engineer I", "3+ years of experience preferred") == (
            Level.ENTRY
        )
        assert resolve_level("New Grad Software Engineer", "5 years of experience") == Level.ENTRY

    def test_years_still_decide_when_the_title_is_silent(self) -> None:
        assert resolve_level("Software Engineer", "6+ years of experience") == Level.SENIOR
        assert resolve_level("Software Engineer", "1+ years of experience") == Level.ENTRY

    def test_clearance_words_are_boundary_anchored(self) -> None:
        assert screen_eligibility("We value transparency and trust.").clearance_required is False
        assert screen_eligibility("Requires a Public Trust clearance.").clearance_required is True


class TestReasonsAreAddressablePerGate:
    """A posting can fail several gates; a grouped UI must show the right evidence."""

    def test_each_gate_gets_its_own_explanation(self) -> None:
        decision = screen(
            make(
                "Principal Software Engineer",
                desc="US citizens only. Requires an active Top Secret clearance.",
            )
        )
        by_gate = decision.reasons_by_gate
        assert "seniority band is senior" in by_gate["wrong_seniority_band"]
        assert "clearance required" in by_gate["security_clearance_required"]
        assert "citizenship/ITAR" in by_gate["citizenship_or_itar_restricted"]

    def test_evidence_quotes_the_matching_phrase_not_another_gates(self) -> None:
        decision = screen(
            make("Software Engineer II", desc="This role is subject to ITAR restrictions.")
        )
        by_gate = decision.reasons_by_gate
        assert "ITAR" in by_gate["citizenship_or_itar_restricted"]
        assert "seniority" in by_gate["wrong_seniority_band"]
        assert by_gate["citizenship_or_itar_restricted"] != by_gate["wrong_seniority_band"]

    def test_reasons_list_still_works(self) -> None:
        decision = screen(make("Senior Software Engineer", desc="Python."))
        assert len(decision.reasons) == len(decision.exclusions)


class TestLevelProvenance:
    """A UI holding only the band will invent a reason for it. Ship the reason."""

    def test_title_marker_is_recorded_as_the_source(self) -> None:
        v = decide_level("New Grad Software Engineer", "Build things.")
        assert v.level is Level.ENTRY
        assert v.source is LevelSource.TITLE
        assert "entry marker" in v.explain()

    def test_years_are_recorded_when_the_title_is_silent(self) -> None:
        """The real retell-ai case: a bare 'Software Engineer' at 1 year."""
        v = decide_level("Software Engineer", "We want 1+ years of experience.")
        assert v.level is Level.ENTRY
        assert v.source is LevelSource.YEARS
        assert v.evidence == "1+ year"
        assert v.explain() == "the description asks for 1+ year"

    def test_neither_source_is_stated_honestly(self) -> None:
        v = decide_level("Software Engineer", "Join our team.")
        assert v.level is Level.UNKNOWN
        assert v.source is LevelSource.NONE
        assert "neither" in v.explain()

    def test_no_description_cannot_claim_a_years_source(self) -> None:
        v = decide_level("Software Engineer", "", desc_available=False)
        assert v.source is LevelSource.NONE

    def test_screen_carries_the_verdict(self) -> None:
        d = screen(make("Software Engineer", desc="Requires 2 years of experience."))
        assert d.level_verdict is not None
        assert d.level_verdict.source is LevelSource.YEARS


class TestFunnelArithmetic:
    def test_gate_counts_overcount_and_are_labelled_separately(self) -> None:
        """A posting failing 3 gates increments 3 counters but is removed once."""
        postings = [
            make("Principal Engineer", desc="US citizens only. Top Secret clearance required."),
            make("New Grad Software Engineer", desc="Python."),
        ]
        _, _, report = screen_all(postings)
        assert report.total == 2
        assert report.kept == 1
        assert report.excluded == 1
        assert report.gate_count_total > report.excluded
