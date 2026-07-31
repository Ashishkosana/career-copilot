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
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import level_batch
import pytest

from copilot.domain.posting import Posting
from copilot.domain.seniority import Level
from copilot.ports.interpreter import Confidence, Interpretation
from copilot.ports.postingstore import ScreenedPage, ScreenedRow, ScreenSummary

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
    """A ``PostingStorePort`` this script can drive, with a real interpretation cache.

    The unused half is stubbed rather than omitted so the fake genuinely *satisfies*
    the Protocol. A fake that only implements the methods under test needs a
    ```` at every call site, and those ignores are what let a
    double drift away from the port it doubles — which in this repo already shipped a
    key-schema mismatch to production behind a fake that validated nothing.
    """

    def __init__(self, postings: list[Posting]) -> None:
        self._postings = postings
        self.cache: dict[str, dict[str, Any]] = {}
        #: How many times the corpus was read. Screening it is 1.5 ms a posting, ~75 s
        #: over the deployed 47,550, so "how many times" is a correctness-shaped
        #: property of a multi-round run rather than a micro-optimisation.
        self.reads = 0

    def open_postings(self) -> list[Posting]:
        self.reads += 1
        return list(self._postings)

    def prepend(self, *postings: Posting) -> None:
        """Rows that sort *ahead* of an exported batch — what the 14:00 fetch cron does."""
        self._postings[:0] = list(postings)

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        return self.cache.get(posting_id)

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        self.cache[posting_id] = payload

    # --- the rest of the port. Present so this satisfies the Protocol; a call to any
    # of them from level_batch would be a bug, so they say so rather than returning a
    # plausible empty value that would pass a test while hiding the mistake.
    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        raise NotImplementedError("level_batch never writes the corpus")

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        raise NotImplementedError("level_batch never closes a posting")

    def new_since(self, since: datetime) -> list[Posting]:
        raise NotImplementedError("level_batch reads every open posting, not a window")

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        raise NotImplementedError("level_batch hydrates from open_postings")

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        raise NotImplementedError("nothing here records an application")

    def save_screening(self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary) -> None:
        raise NotImplementedError("only the cron publishes a screening view")

    def screening_summary(self) -> ScreenSummary | None:
        raise NotImplementedError("level_batch screens directly, it does not read a view")

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> ScreenedPage:
        raise NotImplementedError("level_batch screens directly, it does not read a view")


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


def _import(store: FakeStore, answers: list[Any], tmp_path: Path) -> int:
    path = tmp_path / "round-01.raw.txt"
    path.write_text(json.dumps({"answers": answers}))
    return level_batch._import(store, [path])


def _answer_round(shard: Path, *, evidence: str = "") -> Path:
    """The raw file a model would return for one exported round.

    ``evidence`` defaults to empty, which is the honest answer for a description that
    states no level and the one the verifier has nothing to check.
    """
    postings = json.loads(shard.read_text())["postings"]
    raw = shard.with_name(f"{shard.stem}.raw.txt")
    raw.write_text(
        json.dumps(
            {
                "answers": [
                    {
                        "id": p["id"],
                        "band": "entry",
                        "min_years": 1,
                        "evidence": evidence,
                        "confidence": "high",
                    }
                    for p in postings
                ]
            }
        )
    )
    return raw


class TestExportOffersOnlyWhatNeedsAsking:
    def test_a_posting_with_a_level_in_its_title_is_not_offered(self) -> None:
        """The rules already answered it; asking a model would spend a call to agree."""
        store = FakeStore([posting(1, title="Senior Software Engineer")])
        assert level_batch._needs_level(store, 60) == []

    def test_an_ineligible_posting_is_not_offered(self) -> None:
        """Level is the *last* question, asked only of roles someone could take.

        Refining the seniority of a role that requires citizenship spends a model call
        on a posting that can never enter the worklist.
        """
        store = FakeStore([posting(1, desc=DESC + " Must be a US citizen.")])
        assert level_batch._needs_level(store, 60) == []

    def test_an_unmarked_eligible_posting_is_offered_with_its_description(self) -> None:
        store = FakeStore([posting(1)])
        todo = level_batch._needs_level(store, 60)
        assert len(todo) == 1
        assert set(todo[0]) == {"id", "title", "company", "description"}
        assert REAL_SPAN in todo[0]["description"]

    def test_the_limit_bounds_the_batch(self) -> None:
        """2,207 full descriptions do not fit in one Claude Code context."""
        store = FakeStore([posting(n) for n in range(20)])
        assert len(level_batch._needs_level(store, 5)) == 5

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

        assert len(level_batch._needs_level(store, 60)) == 1

    def test_a_current_row_is_not_offered_again(self) -> None:
        store = FakeStore([posting(1)])
        pid = store.open_postings()[0].id
        store.cache[pid] = Interpretation(band=Level.ENTRY).to_payload()

        assert level_batch._needs_level(store, 60) == []


class TestOneRunDrainsManyRounds:
    """The backlog is 2,207 postings and one model call holds ~25 descriptions.

    So a run is sequential rounds, and the things that can go wrong are all about the
    seam between them: work offered twice, work banked twice, and — the expensive one —
    the corpus screened once per round instead of once per run.
    """

    def test_the_queue_is_sharded_into_rounds_that_do_not_overlap(self, tmp_path: Path) -> None:
        """A posting asked about in two rounds is a model call spent to agree with itself."""
        store = FakeStore([posting(n) for n in range(10)])
        level_batch._export(store, tmp_path, 10, 4)

        shards = level_batch._shards(tmp_path)
        batches = [json.loads(s.read_text())["postings"] for s in shards]
        assert [len(b) for b in batches] == [4, 4, 2]

        ids = [p["id"] for batch in batches for p in batch]
        assert len(ids) == len(set(ids)) == 10

    def test_the_limit_bounds_the_run_and_the_chunk_bounds_the_round(
        self, tmp_path: Path
    ) -> None:
        """Two separate ceilings: the run's wall clock, and one call's context."""
        store = FakeStore([posting(n) for n in range(50)])
        level_batch._export(store, tmp_path, 9, 4)

        batches = [json.loads(s.read_text())["postings"] for s in level_batch._shards(tmp_path)]
        assert [len(b) for b in batches] == [4, 4, 1]

    def test_a_run_that_asks_for_more_than_the_ceiling_is_clamped(self, tmp_path: Path) -> None:
        """Rounds are sequential, so --limit is a wall-clock knob at ~0.9 s a posting.

        A mistyped 100000 must not launch a six-hour run. Clamped rather than refused:
        the useful behaviour for a fat input is to drain the ceiling, not nothing.
        """
        store = FakeStore([posting(n) for n in range(level_batch.LIMIT_CEILING + 20)])
        level_batch._export(store, tmp_path, 100_000, 100)

        exported = sum(
            len(json.loads(s.read_text())["postings"]) for s in level_batch._shards(tmp_path)
        )
        assert exported == level_batch.LIMIT_CEILING

    def test_a_previous_runs_files_do_not_survive_into_this_one(self, tmp_path: Path) -> None:
        """A leftover round-07.raw.txt would be imported as if this run had produced it.

        Worse, a leftover prompt would be answered again — a model call spent on
        postings that are already cached.
        """
        store = FakeStore([posting(n) for n in range(10)])
        level_batch._export(store, tmp_path, 10, 4)
        for shard in level_batch._shards(tmp_path):
            _answer_round(shard)
        stale = sorted(p.name for p in tmp_path.iterdir())

        level_batch._export(store, tmp_path, 2, 4)

        assert len(stale) == 6  # 3 rounds, 3 answers
        assert sorted(p.name for p in tmp_path.iterdir()) == ["round-01.json"]

    def test_every_round_is_imported_and_counted(self, tmp_path: Path) -> None:
        store = FakeStore([posting(n) for n in range(10)])
        level_batch._export(store, tmp_path, 10, 4)
        for shard in level_batch._shards(tmp_path):
            _answer_round(shard)

        rc = level_batch._import(store, level_batch._raw_files(tmp_path))

        assert rc == 0
        assert len(store.cache) == 10

    def test_a_round_that_came_back_as_prose_does_not_discard_the_others(
        self, tmp_path: Path
    ) -> None:
        """Round 2 failing must not cost rounds 1 and 3. They are model spend already made.

        The run still reports red — the model step exits non-zero, and an import where
        *nothing* landed exits non-zero — but banked answers stay banked.
        """
        store = FakeStore([posting(n) for n in range(9)])
        level_batch._export(store, tmp_path, 9, 3)
        shards = level_batch._shards(tmp_path)
        _answer_round(shards[0])
        (tmp_path / "round-02.raw.txt").write_text("I was unable to read those postings.")
        _answer_round(shards[2])

        rc = level_batch._import(store, level_batch._raw_files(tmp_path))

        assert rc == 0, "six good answers is not a failed import"
        assert len(store.cache) == 6
        unanswered = {p["id"] for p in json.loads(shards[1].read_text())["postings"]}
        assert unanswered.isdisjoint(store.cache), "round 2 was not invented"

    def test_an_unanswered_round_is_simply_offered_again(self, tmp_path: Path) -> None:
        """How a stopped run recovers: nothing to reconcile, because the queue *is* the cache.

        Membership is re-derived from what is still uncached, so the rounds a run did
        not reach come back on the next one and the rounds it banked do not.
        """
        store = FakeStore([posting(n) for n in range(9)])
        level_batch._export(store, tmp_path, 9, 3)
        shards = level_batch._shards(tmp_path)
        banked = {p["id"] for p in json.loads(shards[0].read_text())["postings"]}
        _answer_round(shards[0])
        level_batch._import(store, level_batch._raw_files(tmp_path))

        level_batch._export(store, tmp_path, 9, 3)
        offered = {
            p["id"]
            for shard in level_batch._shards(tmp_path)
            for p in json.loads(shard.read_text())["postings"]
        }

        assert offered.isdisjoint(banked), "a cached posting was offered a second time"
        assert len(offered) == 6

    def test_the_corpus_is_screened_once_per_run_not_once_per_round(
        self, tmp_path: Path
    ) -> None:
        """The reason this is one export of many rounds rather than a loop of exports.

        Screening is 1.5 ms a posting — 40 s over the 25,294-posting local corpus, ~75 s
        over the deployed 47,550 — and an export/import loop pays it twice per round.
        Forty rounds that way is over two hours of screening to buy 30 minutes of model
        work, which is how a 25-minute job turns into a timeout.
        """
        store = FakeStore([posting(n) for n in range(20)])
        level_batch._export(store, tmp_path, 20, 2)
        assert len(level_batch._shards(tmp_path)) == 10
        assert store.reads == 1

        for shard in level_batch._shards(tmp_path):
            _answer_round(shard)
        store.reads = 0
        level_batch._import(store, level_batch._raw_files(tmp_path))

        assert store.reads == 1, "ten rounds screened the corpus ten times"

    def test_a_drained_queue_costs_no_model_call_and_no_screen(self, tmp_path: Path) -> None:
        """The normal state once the backlog is gone. It must be free and green."""
        store = FakeStore([posting(1, title="Senior Software Engineer")])
        level_batch._export(store, tmp_path, 150, 25)

        assert level_batch._shards(tmp_path) == []
        assert level_batch._count(tmp_path) == 0
        assert level_batch._prompt(tmp_path) == 0
        assert list(tmp_path.iterdir()) == [], "a prompt was built with nothing to ask"

        store.reads = 0
        assert level_batch._import(store, level_batch._raw_files(tmp_path)) == 0
        assert store.reads == 0, "an empty import screened the corpus for nothing"

    def test_a_prompt_is_built_per_round_and_carries_that_rounds_postings(
        self, tmp_path: Path
    ) -> None:
        store = FakeStore([posting(n) for n in range(5)])
        level_batch._export(store, tmp_path, 5, 2)
        level_batch._prompt(tmp_path)

        prompts = sorted(tmp_path.glob("round-*.prompt.txt"))
        assert [p.name for p in prompts] == [
            "round-01.prompt.txt",
            "round-02.prompt.txt",
            "round-03.prompt.txt",
        ]
        first = prompts[0].read_text()
        assert first.startswith(level_batch.PROMPT_HEADER)
        shards = [json.loads(s.read_text())["postings"] for s in level_batch._shards(tmp_path)]
        assert all(p["id"] in first for p in shards[0])
        assert not any(p["id"] in first for p in shards[1]), "a round leaked into another"

    def test_a_full_round_stays_inside_the_prompt_budget(self, tmp_path: Path) -> None:
        """Two ceilings a silent overflow would cross, so the size is asserted, not assumed.

        A round of 25 descriptions at the 6,000-char cap is the worst case the export
        can produce. It has to stay well inside the model's context — a call that
        overflows returns worse answers, not an error — and inside Linux's 131,072-byte
        limit on a single argv string, which is what forced the prompt onto stdin: the
        old `claude --print "$(cat prompt.txt)"` form dies with E2BIG on the runner and
        never on a Mac.
        """
        big = "We need a software engineer. " * 400  # > MAX_DESCRIPTION_CHARS
        store = FakeStore([posting(n, desc=big) for n in range(level_batch.DEFAULT_CHUNK)])
        level_batch._export(
            store, tmp_path, level_batch.DEFAULT_CHUNK, level_batch.DEFAULT_CHUNK
        )
        level_batch._prompt(tmp_path)

        (prompt,) = tmp_path.glob("round-*.prompt.txt")
        size = len(prompt.read_text().encode())
        cap = level_batch.DEFAULT_CHUNK * level_batch.MAX_DESCRIPTION_CHARS
        assert size < 2 * cap, "a round grew past the evidence it is allowed to carry"
        # ~40k tokens at 4 chars each: a fifth of a 200k window.
        assert size < 200_000

    def test_an_answer_is_found_by_id_not_by_its_place_in_the_queue(
        self, tmp_path: Path
    ) -> None:
        """The bug the id-keyed source map fixes.

        ``import`` used to re-derive "the first --limit postings" and use that window as
        its allowlist, which assumed the queue had not moved since ``export``. It moves:
        the fetch cron runs an hour before this and postings close all day. Anything
        that shifted the order pushed the tail of the batch out of the window, and real
        answers for real postings were rejected as ``unknown_id`` — silently, as a tally
        line. Membership is now "still an eligible, unknown-level, uncached posting",
        which is the property that was meant all along.
        """
        store = FakeStore([posting(n) for n in range(3)])
        level_batch._export(store, tmp_path, 3, 3)
        (shard,) = level_batch._shards(tmp_path)
        last = json.loads(shard.read_text())["postings"][-1]["id"]
        _answer_round(shard, evidence=REAL_SPAN)

        store.prepend(posting(90), posting(91), posting(92))  # newer rows sort ahead

        level_batch._import(store, level_batch._raw_files(tmp_path))

        saved = Interpretation.from_payload(store.cache[last])
        assert saved is not None, "a real answer was refused for being late in the queue"
        assert saved.evidence_verified is True

    def test_a_round_cut_off_mid_answer_is_refused_whole(self, tmp_path: Path) -> None:
        """The failure mode a bigger round would make likelier, and why 25 is the size.

        If a round's output is truncated — the call dies, the output cap is hit — the
        JSON scan finds the *first answer object* rather than the array, because that is
        the first thing in the text that parses. That must not import as "one answer
        for this round": a cut-off round is not evidence of anything, and its postings
        are still uncached, so the next run simply offers them again.
        """
        store = FakeStore([posting(n) for n in range(3)])
        level_batch._export(store, tmp_path, 3, 3)
        (shard,) = level_batch._shards(tmp_path)
        raw = _answer_round(shard, evidence=REAL_SPAN)
        raw.write_text(raw.read_text()[: len(raw.read_text()) // 2])  # cut mid-answer

        rc = level_batch._import(store, [raw])

        assert rc == 1
        assert store.cache == {}, "half a round was imported as a whole one"


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
        assert level_batch._import(FakeStore([]), [path]) == 1

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
        assert level_batch._import(FakeStore([]), [path]) == 1
