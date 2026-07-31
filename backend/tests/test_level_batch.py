"""The Claude Code batch route for reading seniority levels.

This route exists because a Claude subscription token (``sk-ant-oat01-``) cannot call
the Messages API — only a Console ``sk-ant-api03-`` key can, which is a billing
boundary rather than a technical one. So the model runs where that token *is* valid,
inside Claude Code, and this script is the two ends of the trip.

Which means the answers arrive as a **file a model wrote**, with no transport
enforcing anything. Every test here is about that: the file is untrusted input, and
the one property that matters is that a model cannot promote a posting by asserting
something the posting does not say.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import level_batch
import pytest

from copilot.domain.posting import Posting
from copilot.domain.seniority import Level
from copilot.ports.interpreter import Confidence, Interpretation

NOW = datetime(2026, 7, 31, tzinfo=UTC)

#: The exact shape this route exists for: the title states no level **and** the
#: description states none the regex can read — no digits, no "junior", no "senior".
#: A human sees "at the start of their career" immediately, which is the whole reason
#: a model is worth asking. A description containing "1+ years" would be resolved by
#: the rule tier and never offered here at all.
DESC = (
    "We are hiring a software engineer for our platform team. You will build REST "
    "APIs in Python on AWS. This role suits someone at the start of their career "
    "and mentorship is provided."
)

#: A span that really occurs in DESC, for the tests that need a genuine quote.
REAL_SPAN = "suits someone at the start of their career"


class FakeStore:
    """Enough of ``PostingStorePort`` for this script, plus a real cache."""

    def __init__(self, postings: list[Posting]) -> None:
        self._postings = postings
        self.cache: dict[str, dict[str, Any]] = {}

    def open_postings(self) -> list[Posting]:
        return list(self._postings)

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        return self.cache.get(posting_id)

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        self.cache[posting_id] = payload


def posting(n: int = 1, *, title: str = "Software Engineer", desc: str = DESC) -> Posting:
    return Posting(
        title=title,
        company="Acme",
        url=f"https://boards.example/{n}",
        ats="greenhouse",
        location="Remote (US)",
        description=desc,
        desc_available=True,
        employment_type="FullTime",
        posted_at=NOW,
    )


def _import(store: FakeStore, answers: list[Any], tmp_path: Path, limit: int = 60) -> int:
    path = tmp_path / "done.json"
    path.write_text(json.dumps({"answers": answers}))
    return level_batch._import(store, path, limit)  # type: ignore[arg-type]


class TestExportOffersOnlyWhatNeedsAsking:
    def test_a_posting_with_a_level_in_its_title_is_not_offered(self) -> None:
        """The rules already answered it; asking a model would spend a call to agree."""
        store = FakeStore([posting(1, title="Senior Software Engineer")])
        assert level_batch._needs_level(store, 60) == []  # type: ignore[arg-type]

    def test_an_ineligible_posting_is_not_offered(self) -> None:
        """Level is the *last* question, asked only of roles someone could take.

        Refining the seniority of a role that requires citizenship spends a model call
        on a posting that can never enter the worklist.
        """
        store = FakeStore([posting(1, desc=DESC + " Must be a US citizen.")])
        assert level_batch._needs_level(store, 60) == []  # type: ignore[arg-type]

    def test_an_unmarked_eligible_posting_is_offered_with_its_description(self) -> None:
        store = FakeStore([posting(1)])
        todo = level_batch._needs_level(store, 60)  # type: ignore[arg-type]
        assert len(todo) == 1
        assert set(todo[0]) == {"id", "title", "company", "description"}
        assert REAL_SPAN in todo[0]["description"]

    def test_the_limit_bounds_the_batch(self) -> None:
        """631 full descriptions do not fit in one Claude Code context."""
        store = FakeStore([posting(n) for n in range(20)])
        assert len(level_batch._needs_level(store, 5)) == 5  # type: ignore[arg-type]

    def test_a_stale_schema_row_is_offered_again(self) -> None:
        """The landmine this route had to route around.

        ``uncached_ids`` calls a row cached whenever the column is non-empty, while
        ``from_payload`` refuses any row whose schema is not current — so 400 rows in
        the real corpus were counted as done and rejected on read, reachable by no
        batch. Membership here is decided by whether the payload actually *rebuilds*.
        """
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        store.cache[pid] = {"band": "entry", "confidence": "high"}  # no "schema" key

        assert len(level_batch._needs_level(store, 60)) == 1  # type: ignore[arg-type]

    def test_a_current_row_is_not_offered_again(self) -> None:
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        store.cache[pid] = Interpretation(band=Level.ENTRY).to_payload()

        assert level_batch._needs_level(store, 60) == []  # type: ignore[arg-type]


class TestAnswersAreUntrustedInput:
    """A model wrote the file. Nothing in the transport checks anything."""

    def test_a_quote_that_is_not_in_the_description_cannot_assert_a_band(
        self, tmp_path: Path
    ) -> None:
        """The attack this route must survive.

        A model that invents "great entry-level role for new graduates" would promote
        a senior posting into the worklist. The claim is kept — it may be right — but
        stripped of confidence and flagged, so nothing downstream treats it as
        supported.
        """
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        _import(
            store,
            [{"id": pid, "band": "entry", "min_years": 0,
              "evidence": "a great entry-level role for new graduates",
              "confidence": "high"}],
            tmp_path,
        )

        saved = Interpretation.from_payload(store.cache[pid])
        assert saved is not None
        assert saved.evidence_verified is False
        assert saved.confidence is Confidence.LOW, "an unsupported claim cannot stay high"

    def test_a_real_quote_keeps_its_confidence(self, tmp_path: Path) -> None:
        """The control. Validators that reject everything are useless."""
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        _import(
            store,
            [{"id": pid, "band": "entry", "min_years": 1,
              "evidence": REAL_SPAN, "confidence": "high"}],
            tmp_path,
        )

        saved = Interpretation.from_payload(store.cache[pid])
        assert saved is not None
        assert saved.evidence_verified is True
        assert saved.confidence is Confidence.HIGH

    def test_years_that_contradict_the_band_lose_confidence(self, tmp_path: Path) -> None:
        """7 years called entry means one of the two is wrong."""
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        _import(
            store,
            [{"id": pid, "band": "entry", "min_years": 7,
              "evidence": REAL_SPAN, "confidence": "high"}],
            tmp_path,
        )
        saved = Interpretation.from_payload(store.cache[pid])
        assert saved is not None
        assert saved.confidence is not Confidence.HIGH

    def test_an_id_the_export_never_offered_is_refused(self, tmp_path: Path) -> None:
        """Otherwise a file could write a verdict for any posting in the corpus."""
        store = FakeStore([posting(1)])
        _import(store, [{"id": "not-a-real-id", "band": "entry"}], tmp_path)
        assert store.cache == {}

    @pytest.mark.parametrize(
        "answer",
        [
            {"band": "entry"},                       # no id
            {"id": "x", "band": "wizard"},           # not a Level
            {"id": "x", "band": "entry", "confidence": "certain"},
            {"id": "x", "band": "entry", "min_years": "three"},
            {"id": "x", "band": "entry", "evidence": {"nested": "object"}},
            "a bare string",
            None,
        ],
    )
    def test_malformed_answers_are_counted_not_crashed_on(
        self, answer: Any, tmp_path: Path
    ) -> None:
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        if isinstance(answer, dict) and answer.get("id") == "x":
            answer = {**answer, "id": pid}

        _import(store, [answer], tmp_path)  # must not raise

        assert store.cache == {} or Interpretation.from_payload(store.cache[pid]) is not None

    def test_a_file_that_is_not_json_is_an_error_not_a_crash(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json at all")
        assert level_batch._import(FakeStore([]), path, 60) == 1  # type: ignore[arg-type]  # type: ignore[arg-type]

    def test_answers_saving_nothing_exits_non_zero(self, tmp_path: Path) -> None:
        """So a scheduled run reports red rather than succeeding quietly."""
        store = FakeStore([posting(1)])
        assert _import(store, [{"id": "nope", "band": "entry"}], tmp_path) == 1

    def test_an_empty_answer_list_is_not_a_failure(self, tmp_path: Path) -> None:
        """Nothing to do is a legitimate morning once the queue is drained."""
        assert _import(FakeStore([posting(1)]), [], tmp_path) == 0

    def test_the_description_comes_from_the_store_not_the_answers_file(
        self, tmp_path: Path
    ) -> None:
        """Otherwise verification checks a quote against text the model supplied.

        A file carrying both the claim and its own supporting description would pass
        any check, which is why ``_import`` re-derives the sources itself.
        """
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        _import(
            store,
            [{"id": pid, "band": "senior", "evidence": "10+ years required",
              "confidence": "high",
              "description": "10+ years required"}],  # smuggled
            tmp_path,
        )
        saved = Interpretation.from_payload(store.cache[pid])
        assert saved is not None
        assert saved.evidence_verified is False, "the smuggled description was trusted"


class TestModelOutputIsLocatedNotAssumed:
    """A model asked for "JSON only" still sometimes adds prose or a fence.

    Failing the run over a stray "Here you go:" would discard answers that are
    otherwise fine, so the payload is found rather than assumed to be the whole
    output. Every case here is real model behaviour.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            '{"answers": []}',
            'Here you go:\n{"answers": []}',
            '```json\n{"answers": []}\n```',
            '```\n{"answers": []}\n```\nLet me know if you need more.',
            'I read 3 postings.\n\n{"answers": []}\n\nTwo were unclear.',
            '[]',
        ],
    )
    def test_the_payload_is_found_inside_whatever_the_model_said(self, raw: str) -> None:
        assert level_batch.extract_json(raw) is not None

    def test_a_brace_inside_a_quoted_span_does_not_break_it(self) -> None:
        """Descriptions contain braces. Brace-matching by regex would truncate here."""
        raw = 'ok:\n{"answers":[{"id":"a","band":"entry","evidence":"use ${VAR} in config"}]}'
        found = level_batch.extract_json(raw)
        assert found["answers"][0]["evidence"] == "use ${VAR} in config"

    def test_output_with_no_json_at_all_is_an_error(self) -> None:
        assert level_batch.extract_json("I could not complete that request.") is None

    def test_a_file_of_prose_exits_non_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "raw.txt"
        path.write_text("Sorry, I was unable to read the postings.")
        assert level_batch._import(FakeStore([]), path, 60) == 1  # type: ignore[arg-type]
