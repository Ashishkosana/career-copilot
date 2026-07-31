"""Tests for the variant bank and its selector.

Each test names the failure it prevents. The ones that matter most here are not the
"it picks the better variant" cases — those are easy — but these four:

* a variant that contradicts its own declarations must die at **load**, because the
  alternative is discovering it while rendering a résumé for a posting he is about
  to apply to, and the tempting fix at that moment is to ship it;
* the selector must not be allowed to overflow the character budget, since +6
  characters on one real bullet produces a two-page résumé;
* ties must not resolve by dict, document or set iteration order, or "same input,
  same résumé" is false and nothing about the output is defensible;
* an empty requirement set must be a **no-op** — no posting signal means send the
  résumé he already has.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from copilot.domain.bank import (
    Bank,
    BankError,
    BankValidationError,
    load_bank,
    normalise_requirements,
    select,
)
from copilot.domain.gap import tokens_in

# tests/ -> backend/ -> career-copilot/
CONTENT = Path(__file__).resolve().parents[2] / "private" / "resume" / "content.json"


def doc(*bullets: dict[str, Any], date_start: str = "2026-01") -> dict[str, Any]:
    """A one-entry bank document around the given bullet declarations."""
    return {
        "schema_version": 1,
        "entries": [
            {
                "id": "exp.test",
                "kind": "experience",
                "role": "Software Engineer",
                "org": "Test",
                "date_start": date_start,
                "date_end": None,
                "bullets": list(bullets),
            }
        ],
    }


# The baseline says Python + PostgreSQL; one authored variant also says Redis.
B1: dict[str, Any] = {
    "id": "t.b1",
    "text": "Built a payments service in Python on PostgreSQL with a transactional outbox.",
    "tech": ["Python", "PostgreSQL"],
    "metrics": [],
    "allowed_verbs": ["Built"],
    "must_keep_verbatim": ["transactional outbox"],
    "variants": [
        {
            "label": "backend-leaning",
            "text": (
                "Built a payments service in Python on PostgreSQL and Redis with a "
                "transactional outbox."
            ),
            "tech": ["Python", "PostgreSQL", "Redis"],
        }
    ],
}


def test_a_bullet_without_variants_is_a_one_variant_bullet() -> None:
    """The content authored before this module existed must load unchanged.

    If this breaks, every existing bullet needs a migration, and a migration of
    hand-authored résumé prose is exactly where a false claim gets introduced.
    """
    bank = load_bank(doc({k: v for k, v in B1.items() if k != "variants"}))
    bullet = bank.bullet("t.b1")
    assert len(bullet.variants) == 1
    assert bullet.baseline.variant_id == "t.b1#base"
    assert bank.baseline_texts()["t.b1"] == B1["text"]


#: Two bullets, each able to surface exactly one of two requirements. The
#: non-baseline variant is deliberately *shorter*, so "shortest wins" and
#: "authored order wins" give different answers.
TIE: dict[str, Any] = {
    "id": "t.b2",
    "text": "Containerised the service with Docker and wrote the deployment manifests.",
    "tech": ["Docker"],
    "metrics": [],
    "allowed_verbs": ["Containerised"],
    "must_keep_verbatim": ["deployment manifests"],
    "variants": [
        {
            "text": "Containerised the service on Kubernetes deployment manifests.",
            "tech": ["Kubernetes"],
        }
    ],
}

#: Baseline is short and covers Python; the variant covers Python + Redis but
#: is longer. Which one ships depends only on the budget.
GROWS: dict[str, Any] = {
    "id": "t.b3",
    "text": "Built the ingest worker in Python with a retry queue.",
    "tech": ["Python"],
    "metrics": [],
    "allowed_verbs": ["Built"],
    "must_keep_verbatim": ["retry queue"],
    "variants": [
        {
            "text": "Built the ingest worker in Python on Redis with a retry queue.",
            "tech": ["Python", "Redis"],
        }
    ],
}

PIPELINE: dict[str, Any] = {
    "id": "t.b1",
    "text": "Built the event pipeline in Python with an outbox relay.",
    "tech": ["Python"],
    "metrics": [],
    "allowed_verbs": ["Built"],
    "must_keep_verbatim": ["outbox relay"],
    "variants": [
        {
            "text": (
                "Built the event pipeline in Python on Redis and Kafka with an outbox relay."
            ),
            "tech": ["Python", "Redis", "Kafka"],
        }
    ],
}
CACHE: dict[str, Any] = {
    "id": "t.b2",
    "text": "Wrote the cache layer with a write-through policy.",
    "tech": [],
    "metrics": [],
    "allowed_verbs": ["Wrote"],
    "must_keep_verbatim": ["write-through"],
    "variants": [
        {
            "text": "Wrote the Redis cache layer with a write-through policy.",
            "tech": ["Redis"],
        }
    ],
}


class TestSelection:
    def test_variant_covering_strictly_more_requirements_wins(self) -> None:
        bank = load_bank(doc(B1))
        chosen = select(bank, ["Python", "PostgreSQL", "Redis"])
        (choice,) = chosen.choices
        assert choice.variant_id == "t.b1#v1"
        assert choice.covered == ("PostgreSQL", "Python", "Redis")
        assert choice.newly_covered == ("PostgreSQL", "Python", "Redis")
        assert not chosen.uncovered

    def test_it_says_which_requirement_the_switch_bought(self) -> None:
        """A selector that cannot explain itself is unusable for a document he
        has to defend line by line in an interview."""
        bank = load_bank(doc(B1))
        chosen = select(bank, ["Redis"])
        (choice,) = chosen.choices
        assert choice.newly_covered == ("Redis",)
        assert not choice.is_baseline
        assert "Redis" in choice.reason
        assert "t.b1#v1" in chosen.report()

    def test_empty_requirements_is_a_no_op(self) -> None:
        """No posting signal must mean no change, not "pick something"."""
        bank = load_bank(doc(B1))
        chosen = select(bank, [])
        assert chosen.texts() == bank.baseline_texts()
        assert all(c.is_baseline for c in chosen.choices)
        assert chosen.covered == frozenset()

    def test_a_requirement_no_variant_states_is_reported_not_hidden(self) -> None:
        bank = load_bank(doc(B1))
        chosen = select(bank, ["Python", "Kubernetes"])
        assert chosen.uncovered == frozenset({"Kubernetes"})
        assert "Kubernetes" in chosen.report()
        assert "Kubernetes" not in bank.coverable()

    def test_what_it_claims_to_cover_is_really_in_the_rendered_text(self) -> None:
        """Coverage is measured on the text, not on the ``tech`` allowlist.

        ``tech`` is a ceiling on what *may* appear (v1), not a promise that it
        does: ``exp.crewtron.b5`` declares Git and CI/CD while its text says
        "GitHub Actions", which VOCAB does not match. Reading coverage off the
        allowlist would have the selector report a requirement covered that
        ``gap.build_report`` — which tokenises the rendered résumé — then reports
        missing.
        """
        bank = load_bank(
            doc(
                {
                    "id": "t.b1",
                    "text": "Automated the release pipeline with GitHub Actions.",
                    # Declares far more than the text can be matched for.
                    "tech": ["Git", "CI/CD", "Python"],
                    "metrics": [],
                    "allowed_verbs": ["Automated"],
                    "must_keep_verbatim": ["GitHub Actions"],
                }
            )
        )
        chosen = select(bank, ["Git", "CI/CD"])
        assert chosen.covered == frozenset()
        rendered = tokens_in(" ".join(chosen.texts().values()))
        assert chosen.covered <= rendered


class TestDeterminism:
    def test_a_tie_resolves_to_the_baseline_not_to_the_shorter_text(self) -> None:
        """Equal coverage must not cause churn in the résumé he already has."""
        bank = load_bank(doc(TIE))
        chosen = select(bank, ["Docker", "Kubernetes"])
        (choice,) = chosen.choices
        assert choice.covered == ("Docker",)
        assert choice.is_baseline
        assert choice.variant_id == "t.b2#base"
        # The rejected variant really is shorter, or this test proves nothing.
        alt = bank.bullet("t.b2").variants[1]
        assert len(alt.text) < len(choice.text)

    def test_reordering_the_document_cannot_change_the_selection(self) -> None:
        """Selection order is sorted bullet id, never document or dict order.

        Without this, editing the JSON to move a project above a job silently
        changes which bullet gets credit for a shared requirement.
        """
        first = load_bank(doc(B1, TIE))
        second = load_bank(doc(TIE, B1))
        reqs = ["Python", "Redis", "Docker", "Kubernetes"]
        assert select(first, reqs).texts() == select(second, reqs).texts()
        assert select(first, reqs).variant_ids() == select(second, reqs).variant_ids()

    def test_requirement_iteration_order_cannot_change_the_selection(self) -> None:
        bank = load_bank(doc(B1, TIE))
        forward = select(bank, ["Python", "Redis", "Docker", "Kubernetes"])
        backward = select(bank, ["Kubernetes", "Docker", "Redis", "Python"])
        assert forward.variant_ids() == backward.variant_ids()
        assert forward.covered == backward.covered

    def test_repeated_selection_is_identical(self) -> None:
        bank = load_bank(doc(B1, TIE))
        runs = {select(bank, ["Redis", "Docker"]).report() for _ in range(5)}
        assert len(runs) == 1


class TestBudget:
    def test_a_tight_budget_forces_the_shorter_variant(self) -> None:
        """A character budget is a hard constraint, not a preference.

        Measured per-bullet headroom on the real résumé ranges +1 to +89
        characters; on one bullet +6 characters produces a two-page résumé. So a
        variant that would cover more requirements must lose to the page.
        """
        bank = load_bank(doc(GROWS))
        growth = len(GROWS["variants"][0]["text"]) - len(GROWS["text"])
        assert growth > 0

        tight = select(bank, ["Python", "Redis"], {"t.b3": growth - 1})
        (choice,) = tight.choices
        assert choice.is_baseline
        assert choice.delta_chars == 0
        assert choice.within_budget
        assert tight.uncovered == frozenset({"Redis"})

        roomy = select(bank, ["Python", "Redis"], {"t.b3": growth})
        assert roomy.choices[0].variant_id == "t.b3#v1"
        assert roomy.choices[0].delta_chars == growth

    def test_no_budget_at_all_is_reported_rather_than_assumed(self) -> None:
        """An unmeasured budget cannot be enforced; the caller must be able to see
        that it was never measured instead of trusting a silent default."""
        bank = load_bank(doc(GROWS))
        chosen = select(bank, ["Redis"])
        assert chosen.choices[0].budget is None
        assert chosen.choices[0].variant_id == "t.b3#v1"

    def test_when_nothing_fits_it_shrinks_instead_of_overflowing(self) -> None:
        """A negative budget means this bullet has to *shrink* to hold the page.

        The baseline is then out of budget too, so every variant can miss. The
        contract is fall back to the shortest and say so — never overflow.
        """
        bank = load_bank(doc(GROWS))
        chosen = select(bank, ["Python", "Redis"], {"t.b3": -200})
        (choice,) = chosen.choices
        assert not choice.within_budget
        assert chosen.overflowed == (choice,)
        shortest = min(bank.bullet("t.b3").variants, key=lambda v: len(v.text))
        assert choice.text == shortest.text
        assert "shortest" in choice.reason
        assert "OVER BUDGET" in chosen.report()

    def test_a_budget_on_another_bullet_does_not_constrain_this_one(self) -> None:
        bank = load_bank(doc(GROWS, B1))
        chosen = select(bank, ["Redis"], {"t.b1": -200})
        by_id = {c.bullet_id: c for c in chosen.choices}
        assert not by_id["t.b1"].within_budget
        assert by_id["t.b3"].within_budget


class TestGreedyCoverage:
    """Two bullets that can both say Redis, and only one that can say Kafka."""

    def test_it_does_not_spend_a_second_bullet_on_a_covered_requirement(self) -> None:
        """Per-bullet argmax would switch both bullets and buy nothing.

        Greenhouse keyword matching is exact-match boolean, so a second mention of
        Redis adds no signal — and it costs the résumé line that could have stayed
        the wording he is used to defending.
        """
        bank = load_bank(doc(PIPELINE, CACHE))
        chosen = select(bank, ["Redis", "Kafka"])
        by_id = {c.bullet_id: c for c in chosen.choices}
        assert by_id["t.b1"].variant_id == "t.b1#v1"
        assert by_id["t.b1"].newly_covered == ("Kafka", "Redis")
        assert by_id["t.b2"].is_baseline
        assert by_id["t.b2"].newly_covered == ()
        assert chosen.covered == frozenset({"Redis", "Kafka"})

    def test_the_second_bullet_still_switches_when_it_is_the_only_source(self) -> None:
        bank = load_bank(doc(CACHE))
        chosen = select(bank, ["Redis"])
        assert chosen.choices[0].variant_id == "t.b2#v1"


class TestRejectedAtLoad:
    """Every case here is a badly authored variant. None may survive to render."""

    def _load(self, variant: dict[str, Any]) -> Bank:
        base = {k: v for k, v in B1.items() if k != "variants"}
        return load_bank(doc({**base, "variants": [variant]}))

    def test_variant_naming_a_technology_it_does_not_declare(self) -> None:
        with pytest.raises(BankValidationError) as exc:
            self._load(
                {
                    "text": (
                        "Built a payments service in Python on PostgreSQL and Kubernetes "
                        "with a transactional outbox."
                    )
                    # inherits tech = [Python, PostgreSQL]; Kubernetes is undeclared
                }
            )
        assert "v1-technology" in str(exc.value)
        assert "Kubernetes" in str(exc.value)

    def test_the_error_names_the_variant_not_just_the_bullet(self) -> None:
        """"bullet t.b1 is invalid" does not tell an author which of five
        phrasings to go and fix."""
        with pytest.raises(BankValidationError) as exc:
            self._load({"id": "t.b1#gcp", "text": "Built it on Google Cloud, in Python."})
        assert "t.b1#gcp" in exc.value.result.report()
        assert {v.bullet_id for v in exc.value.result.violations} == {"t.b1#gcp"}

    def test_variant_stating_a_number_it_declares_no_metric_for(self) -> None:
        with pytest.raises(BankValidationError) as exc:
            self._load(
                {
                    "text": (
                        "Built a payments service in Python on PostgreSQL with a "
                        "transactional outbox, cutting latency by 40%."
                    )
                }
            )
        assert "v2-number" in str(exc.value)

    def test_variant_moving_a_declared_number_onto_another_subject(self) -> None:
        """The number is real and verbatim; the claim it decorates is not."""
        with pytest.raises(BankValidationError) as exc:
            load_bank(
                doc(
                    {
                        "id": "t.b4",
                        "text": "Wrote an integration suite reaching 82% coverage.",
                        "tech": [],
                        "metrics": [
                            {
                                "id": "m.cov",
                                "numbers": ["82"],
                                "unit": "percent",
                                "subject_head": "coverage",
                                "claim": "82% coverage",
                                "evidence": "coverage report",
                            }
                        ],
                        "allowed_verbs": ["Wrote"],
                        "must_keep_verbatim": [],
                        "variants": [
                            {"text": "Wrote an integration suite covering 82% of the fleet."}
                        ],
                    }
                )
            )
        assert "v2-subject" in str(exc.value)

    def test_variant_dropping_an_inherited_protected_phrase(self) -> None:
        """Deleting the mechanism fabricates by omission while adding no words.

        Inheritance is what makes this catchable: a variant that omits
        ``must_keep_verbatim`` keeps the bullet's, so silently dropping
        "transactional outbox" is a load-time failure rather than an authoring
        decision nobody reviewed.
        """
        with pytest.raises(BankValidationError) as exc:
            self._load({"text": "Built a payments service in Python on PostgreSQL."})
        assert "v8-must-keep" in str(exc.value)
        assert "transactional outbox" in str(exc.value)

    def test_a_variant_may_override_the_declarations_it_needs_to(self) -> None:
        """The escape hatch must exist, or authors cannot write a shorter variant.

        Dropping a protected phrase is allowed only by saying so explicitly, which
        is the point at which a human has to answer "is it still true?".
        """
        bank = self._load(
            {
                "text": "Built a payments service in Python on PostgreSQL.",
                "must_keep_verbatim": [],
            }
        )
        assert len(bank.bullet("t.b1").variants) == 2

    def test_the_baseline_itself_is_validated(self) -> None:
        """Nothing is grandfathered in because it happens to be index 0."""
        with pytest.raises(BankValidationError) as exc:
            load_bank(
                doc(
                    {
                        "id": "t.b5",
                        "text": "Built it in Python on Kubernetes.",
                        "tech": ["Python"],
                        "metrics": [],
                        "allowed_verbs": ["Built"],
                        "must_keep_verbatim": [],
                    }
                )
            )
        assert "t.b5#base" in str(exc.value)


class TestMalformedDocuments:
    def test_duplicate_bullet_ids_are_refused(self) -> None:
        """Two bullets with one id means the renderer's key is ambiguous and one
        of them silently disappears."""
        with pytest.raises(BankError, match="duplicate bullet id"):
            load_bank(doc(B1, B1))

    def test_duplicate_variant_ids_are_refused(self) -> None:
        base = {k: v for k, v in B1.items() if k != "variants"}
        with pytest.raises(BankError, match="duplicate variant id"):
            load_bank(
                doc(
                    {
                        **base,
                        "variants": [
                            {"id": "t.b1#alt", "text": "Built it in Python, with an outbox."},
                            {"id": "t.b1#alt", "text": "Built it in Python. An outbox is used."},
                        ],
                    }
                )
            )

    def test_an_empty_variant_text_is_refused(self) -> None:
        base = {k: v for k, v in B1.items() if k != "variants"}
        with pytest.raises(BankError, match="text"):
            load_bank(doc({**base, "variants": [{"text": "   "}]}))

    def test_a_variant_block_that_is_not_a_list_is_refused(self) -> None:
        base = {k: v for k, v in B1.items() if k != "variants"}
        with pytest.raises(BankError, match="expected a list"):
            load_bank(doc({**base, "variants": {"text": "Built it in Python."}}))

    def test_a_metric_missing_its_subject_is_refused(self) -> None:
        with pytest.raises(BankError, match="subject_head"):
            load_bank(
                doc(
                    {
                        "id": "t.b6",
                        "text": "Wrote 27 tests.",
                        "tech": [],
                        "metrics": [
                            {
                                "id": "m.t",
                                "numbers": ["27"],
                                "unit": "tests",
                                "claim": "27 tests",
                                "evidence": "pytest",
                            }
                        ],
                        "allowed_verbs": ["Wrote"],
                        "must_keep_verbatim": [],
                    }
                )
            )

    def test_an_empty_bank_selects_nothing_rather_than_raising(self) -> None:
        bank = load_bank({"schema_version": 1, "entries": []})
        chosen = select(bank, ["Python"])
        assert chosen.choices == ()
        assert chosen.uncovered == frozenset({"Python"})


class TestRequirementNormalisation:
    def test_a_lowercase_requirement_still_matches(self) -> None:
        """Requirement sets arrive from GapReport (canonical) and from humans at a
        CLI (not). A case miss looks exactly like "no variant covers it"."""
        assert normalise_requirements(["python", "REDIS"]) == frozenset({"Python", "Redis"})
        bank = load_bank(doc(B1))
        assert select(bank, ["redis"]).choices[0].variant_id == "t.b1#v1"

    def test_an_unknown_requirement_is_kept_so_it_shows_up_as_a_gap(self) -> None:
        chosen = select(load_bank(doc(B1)), ["Elixir", "  ", "Python"])
        assert "Elixir" in chosen.uncovered
        assert chosen.requirements == frozenset({"Elixir", "Python"})


@pytest.mark.skipif(
    not CONTENT.exists(),
    reason="private/resume/content.json is personal and gitignored; see private.example/",
)
class TestRealResume:
    """The real authored content must load as a bank with no changes to it."""

    def test_it_loads_and_every_bullet_has_a_baseline(self) -> None:
        bank = load_bank(json.loads(CONTENT.read_text()))
        assert bank.bullets
        for bullet in bank.bullets:
            assert bullet.baseline.variant_id == f"{bullet.id}#base"

    def test_the_baselines_are_character_identical_to_the_authored_text(self) -> None:
        raw = json.loads(CONTENT.read_text())
        authored = {
            b["id"]: b["text"] for entry in raw["entries"] for b in entry["bullets"]
        }
        assert load_bank(raw).baseline_texts() == authored

    def test_selecting_against_a_real_posting_never_leaves_the_baseline_uncited(self) -> None:
        """Until variants are authored there is exactly one per bullet, so every
        selection must be the baseline — including for a posting that names
        things the résumé does not have."""
        bank = load_bank(json.loads(CONTENT.read_text()))
        chosen = select(bank, ["Python", "Kubernetes", "Go"], {b.id: 0 for b in bank.bullets})
        assert chosen.texts() == bank.baseline_texts()
        assert all(c.within_budget for c in chosen.choices)
        assert "Go" in chosen.uncovered
