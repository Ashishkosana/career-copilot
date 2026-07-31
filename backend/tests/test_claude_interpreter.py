"""Unit tests for the LLM level interpreter.

No network and no API key: every test drives the adapter with an injected fake
client. Each test names the failure it prevents, because the whole point of this
tier is that it is the *only* non-deterministic step in the pipeline and it must
fail safe in every direction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from copilot.adapters import claude_interpreter as mod
from copilot.adapters.claude_interpreter import ClaudeInterpreter
from copilot.domain.posting import Posting
from copilot.domain.seniority import Level
from copilot.ports.interpreter import Confidence, Interpretation

_DESCRIPTION = (
    "About the role\n"
    "You will join the payments platform team and ship backend services.\n"
    "Requirements:\n"
    "- 1+ years of professional software engineering experience\n"
    "- Familiarity with Python or Go\n"
    "We are an equal opportunity employer.\n"
)

_POSTING = Posting(
    title="Software Engineer",
    company="Acme",
    url="https://jobs.example.com/acme/swe",
    ats="greenhouse",
    description=_DESCRIPTION,
)


# --- fakes -------------------------------------------------------------------


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 1370
    output_tokens: int = 120


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"


def _reply(
    *,
    band: str = "entry",
    min_years: int | None = 1,
    evidence: str = "1+ years of professional software engineering experience",
    confidence: str = "high",
) -> _Response:
    payload = {
        "band": band,
        "min_years": min_years,
        "evidence": evidence,
        "confidence": confidence,
    }
    return _Response(content=[_Block(text=json.dumps(payload))])


class _FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if self._outcomes else _reply()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    """Stands in for ``anthropic.Anthropic`` — only ``messages.create`` is used."""

    def __init__(self, *outcomes: Any) -> None:
        self.messages = _FakeMessages(list(outcomes))


class _FakeStore:
    """The two PostingStorePort methods this adapter touches."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.reads = 0

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        self.reads += 1
        return self.rows.get(posting_id)

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        self.rows[posting_id] = json.loads(json.dumps(payload))  # prove JSON-safety

    # Unused by the interpreter, present so the fake satisfies the Protocol.
    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        raise NotImplementedError

    def new_since(self, since: datetime) -> list[Posting]:
        raise NotImplementedError

    def open_postings(self) -> list[Posting]:
        raise NotImplementedError

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        raise NotImplementedError


# --- prompt ------------------------------------------------------------------


def test_build_prompt_carries_title_company_and_description() -> None:
    prompt = ClaudeInterpreter.build_prompt(_POSTING)
    assert "Software Engineer" in prompt
    assert "Acme" in prompt
    assert "1+ years of professional software engineering experience" in prompt


def test_build_prompt_truncates_a_long_description() -> None:
    """A 40k-char Workday description must not silently become a 10k-token bill."""
    long_posting = _POSTING.model_copy(update={"description": "x" * 40_000})
    prompt = ClaudeInterpreter.build_prompt(long_posting, 500)
    assert prompt.count("x") == 500


# --- happy path --------------------------------------------------------------


def test_well_formed_reply_is_parsed_and_verified() -> None:
    client = _FakeClient(_reply())
    interp = ClaudeInterpreter(client=client)

    verdict = interp.interpret(_POSTING)

    assert verdict is not None
    assert verdict.band is Level.ENTRY
    assert verdict.min_years == 1
    assert verdict.confidence is Confidence.HIGH
    assert verdict.evidence_verified is True
    assert verdict.evidence in _DESCRIPTION
    assert interp.stats.calls == 1
    assert interp.stats.unverified == 0


def test_request_asks_for_structured_output_on_the_cheap_model() -> None:
    """Guards the two things that keep this tier cheap and parseable.

    Dropping ``output_config`` makes JSON parsing a failure surface again, and
    silently swapping the model would multiply the bill by 5x-25x per token.
    """
    client = _FakeClient(_reply())
    ClaudeInterpreter(client=client).interpret(_POSTING)

    sent = client.messages.calls[0]
    assert sent["model"] == "claude-haiku-4-5"
    schema = sent["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["schema"]["required"] == ["band", "min_years", "evidence", "confidence"]
    assert schema["schema"]["additionalProperties"] is False
    assert sent["max_tokens"] == 256


def test_usage_is_accumulated_for_cost_reporting() -> None:
    client = _FakeClient(_reply(), _reply())
    interp = ClaudeInterpreter(client=client)

    interp.interpret(_POSTING)
    interp.interpret(_POSTING.model_copy(update={"url": "https://x/2"}))

    assert interp.stats.input_tokens == 2740
    assert interp.stats.output_tokens == 240
    assert interp.stats.estimated_cost_usd() == pytest.approx(2740 / 1e6 + 240 / 1e6 * 5)


# --- the span is an index, not proof ----------------------------------------


def test_evidence_absent_from_description_is_marked_unverified_and_downgraded() -> None:
    """A fabricated citation must never pass as evidence.

    The model returns a fluent, plausible quote that appears nowhere in the
    posting. Without the substring check this reads as a high-confidence ENTRY
    verdict backed by a citation — the exact failure mode of a system that trusts
    a returned span.
    """
    client = _FakeClient(
        _reply(evidence="ideal for recent graduates entering the industry", confidence="high")
    )
    interp = ClaudeInterpreter(client=client)

    verdict = interp.interpret(_POSTING)

    assert verdict is not None
    assert verdict.evidence_verified is False
    assert verdict.confidence is Confidence.LOW
    assert verdict.band is Level.ENTRY  # kept, but no longer trusted
    assert interp.stats.unverified == 1


def test_unknown_band_needs_no_span() -> None:
    """"The description does not say" is a real answer and must not be penalised."""
    client = _FakeClient(_reply(band="unknown", min_years=None, evidence="", confidence="high"))
    interp = ClaudeInterpreter(client=client)

    verdict = interp.interpret(_POSTING)

    assert verdict is not None
    assert verdict.band is Level.UNKNOWN
    assert verdict.confidence is Confidence.HIGH
    assert interp.stats.unverified == 0


def test_reflowed_span_is_accepted_but_downgraded() -> None:
    """HTML descriptions wrap mid-sentence; a re-flowed quote is real but not verbatim."""
    wrapped = _POSTING.model_copy(
        update={"description": "Requirements:\n- 1+ years of\n  professional experience\n"}
    )
    client = _FakeClient(
        _reply(evidence="1+ years of professional experience", confidence="high")
    )
    interp = ClaudeInterpreter(client=client)

    verdict = interp.interpret(wrapped)

    assert verdict is not None
    assert verdict.evidence_verified is True
    assert verdict.confidence is Confidence.MEDIUM
    assert interp.stats.unverified == 0


def test_band_contradicting_its_own_years_is_downgraded() -> None:
    """8 years is not an entry role; one of the two fields is wrong either way."""
    client = _FakeClient(
        _reply(
            band="entry",
            min_years=8,
            evidence="1+ years of professional software engineering experience",
            confidence="high",
        )
    )
    verdict = ClaudeInterpreter(client=client).interpret(_POSTING)

    assert verdict is not None
    assert verdict.confidence is Confidence.MEDIUM


def test_implausible_and_boolean_years_are_dropped() -> None:
    """``isinstance(True, int)`` is True in Python — a bool must not become 1 year."""
    client = _FakeClient(_reply(min_years=99), _reply(min_years=True))
    interp = ClaudeInterpreter(client=client)

    first = interp.interpret(_POSTING)
    second = interp.interpret(_POSTING.model_copy(update={"url": "https://x/2"}))

    assert first is not None and first.min_years is None
    assert second is not None and second.min_years is None


# --- failure modes -----------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        _Response(content=[_Block(text="Sure! The band is entry-level.")]),  # not JSON
        _Response(content=[_Block(text="[1, 2, 3]")]),  # JSON, wrong shape
        _Response(content=[_Block(text='{"band": "junior-ish", "confidence": "high"}')]),
        _Response(content=[]),  # no content block at all
        _Response(content=[_Block(text="tool call", type="tool_use")]),  # no text block
        _Response(content=[_Block(text='{"band": "entry"')], stop_reason="max_tokens"),
    ],
)
def test_malformed_reply_degrades_to_none(response: _Response) -> None:
    """A reply this adapter cannot read is a cache miss, never an exception."""
    interp = ClaudeInterpreter(client=_FakeClient(response))

    assert interp.interpret(_POSTING) is None
    assert interp.stats.failures == 1


def test_api_error_degrades_to_none_without_raising() -> None:
    """One failing posting must not abort a batch of hundreds."""
    interp = ClaudeInterpreter(client=_FakeClient(RuntimeError("529 overloaded")))

    assert interp.interpret(_POSTING) is None
    assert interp.stats.failures == 1


def test_no_api_key_returns_none_and_makes_no_call() -> None:
    """The path that runs today: no key in the environment, rule verdict preserved."""
    interp = ClaudeInterpreter(api_key="")

    assert interp.interpret(_POSTING) is None
    assert interp.stats.calls == 0


def test_missing_sdk_degrades_like_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Lambda bundle is assembled by hand; a missing wheel must not crash the cron."""

    def _boom(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(mod, "import_module", _boom)
    interp = ClaudeInterpreter(api_key="sk-test")

    assert interp.interpret(_POSTING) is None
    assert interp.stats.calls == 0


def test_missing_description_never_spends_a_call() -> None:
    """Workday's list endpoint returns no description; a title-only guess is worthless."""
    interp = ClaudeInterpreter(client=_FakeClient(_reply()))
    blank = _POSTING.model_copy(update={"description": "", "desc_available": False})
    whitespace = _POSTING.model_copy(update={"description": "   \n"})

    assert interp.interpret(blank) is None
    assert interp.interpret(whitespace) is None
    assert interp.stats.calls == 0


# --- caching -----------------------------------------------------------------


def test_second_look_at_the_same_posting_reads_the_cache_not_the_model() -> None:
    """A description must be read once ever, not once per daily run."""
    store = _FakeStore()
    client = _FakeClient(_reply())
    interp = ClaudeInterpreter(client=client, store=store)

    first = interp.interpret(_POSTING)
    second = interp.interpret(_POSTING)

    assert first == second
    assert len(client.messages.calls) == 1
    assert interp.stats.cache_hits == 1


def test_cached_interpretation_survives_a_run_with_no_key() -> None:
    """Cache lookup happens before the credential check, or every run pays again."""
    store = _FakeStore()
    ClaudeInterpreter(client=_FakeClient(_reply()), store=store).interpret(_POSTING)

    keyless = ClaudeInterpreter(api_key="", store=store)
    verdict = keyless.interpret(_POSTING)

    assert verdict is not None
    assert verdict.band is Level.ENTRY
    assert keyless.stats.calls == 0


def test_failed_interpretation_is_not_cached() -> None:
    """Caching a failure would make one bad reply permanent."""
    store = _FakeStore()
    interp = ClaudeInterpreter(client=_FakeClient(RuntimeError("boom"), _reply()), store=store)

    assert interp.interpret(_POSTING) is None
    assert store.rows == {}
    assert interp.interpret(_POSTING) is not None  # retried on the next run


def test_cache_row_from_an_older_payload_shape_is_a_miss_not_a_crash() -> None:
    store = _FakeStore()
    store.rows[_POSTING.id] = {"schema": 0, "band": "senior"}
    interp = ClaudeInterpreter(client=_FakeClient(_reply()), store=store)

    verdict = interp.interpret(_POSTING)

    assert verdict is not None
    assert verdict.band is Level.ENTRY  # re-asked, not deserialised from the old row
    assert interp.stats.cache_hits == 0


def test_interpret_many_keys_by_posting_id_and_omits_unanswerable() -> None:
    answerable = _POSTING
    no_description = _POSTING.model_copy(
        update={"url": "https://x/2", "description": "", "desc_available": False}
    )
    interp = ClaudeInterpreter(client=_FakeClient(_reply()), store=_FakeStore())

    results = interp.interpret_many([answerable, no_description])

    assert set(results) == {answerable.id}
    assert results[answerable.id].band is Level.ENTRY


# --- payload round-trip ------------------------------------------------------


def test_payload_round_trip_preserves_every_field() -> None:
    original = Interpretation(
        band=Level.MID,
        min_years=4,
        evidence="4+ years of experience",
        confidence=Confidence.MEDIUM,
        evidence_verified=True,
    )

    assert Interpretation.from_payload(original.to_payload()) == original


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"schema": 1, "band": "wizard", "confidence": "high"},
        {"schema": 1, "band": "entry", "confidence": "certain"},
        {"schema": 1, "band": "entry", "confidence": "high", "min_years": "two"},
        {"schema": 99, "band": "entry", "confidence": "high", "min_years": 1},
    ],
)
def test_from_payload_rejects_unusable_rows(payload: dict[str, Any] | None) -> None:
    """A persisted cache file outlives the code that wrote it. Never raise on a row."""
    assert Interpretation.from_payload(payload) is None


def test_confidence_downgrade_floors_at_low() -> None:
    assert Confidence.HIGH.downgraded() is Confidence.MEDIUM
    assert Confidence.MEDIUM.downgraded() is Confidence.LOW
    assert Confidence.LOW.downgraded() is Confidence.LOW
