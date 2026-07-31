"""Hosted-LLM implementation of :class:`~copilot.ports.interpreter.InterpreterPort`.

This is the only place in the pipeline where a model decides anything. It exists
because 652 of the 880 eligible postings carry a bare title like "Software
Engineer" and state no years requirement, so neither the title rules nor the
years regex in ``domain.seniority`` can separate a new-grad role from a ten-year
one. The description can. Nothing else here is model-driven.

Three properties make that dependency safe to ship:

**It degrades to nothing.** No key, no SDK in the bundle, an API error, a reply
that will not parse — every one of those returns ``None``, and the caller keeps
the deterministic verdict. The no-key path is the one that runs today.

**It never trusts the span it is given.** The model must quote the substring that
decided the band; this adapter looks that substring up in the description and
downgrades confidence when it is not there. It also re-checks the band against
``level_from_years`` on the years the model reported, using the same pure domain
function the rule tier uses. Both checks are free and both catch real drift.

**It reads a description once, ever.** Every call is behind
``PostingStorePort.cached_interpretation`` keyed on posting id. A posting sits in
a feed for weeks; at ~1,370 input tokens each, re-reading 880 descriptions daily
would cost more than the model choice ever could.

Cost, on ~1,370 input and ~120 output tokens per posting at ``claude-opus-5``
list price ($5.00 / $25.00 per MTok):

* **$9.85 per 1,000 postings** — $6.85 input + $3.00 output.
* The 631-posting backfill: **~$6.22**, once.
* Steady state is the day's genuinely new postings, which the live corpus puts at
  ~358: **~$3.53/day** if every one lacks a title marker, and in practice a
  fraction of that, since most new postings state a level and never reach here.

That is **5x what ``claude-haiku-4-5`` cost** for the same work ($1.97/1,000), and
the choice is deliberate rather than accidental: this is a classification with a
fixed output schema, which is the shape a small model handles well. Opus is used
because it was asked for, and the knob is the ``model`` argument — one string —
if the bill argues otherwise. The per-posting cache is what keeps either number
bounded: a posting is interpreted once, ever, not once per run.

Batching is deliberately *not* used. The Batch API halves the price but allows up
to 24 hours of latency, and at two postings a day it would save $0.002/day while
making a daily briefing arrive a day late. It is worth exactly one thing — the
one-time backfill, where $1.28 becomes $0.64 — which does not justify a second
code path. Prompt caching is likewise skipped and cannot help here: the shared
prefix is the ~280-token system prompt, and the whole request is ~1,370 tokens —
both under the 1,024-token minimum cacheable prefix this tier applies. The bulk of
every request is the description, which differs per posting by definition.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from copilot.domain.posting import Posting
from copilot.domain.seniority import Level, level_from_years
from copilot.logging import get_logger
from copilot.ports.interpreter import Confidence, Interpretation
from copilot.ports.postingstore import PostingStorePort
from copilot.ports.secrets import SecretsPort

_LOG = get_logger("copilot.adapters.claude_interpreter")

#: The direct-key escape hatch. Named here rather than in the resolver because
#: which env var overrides a credential is the caller's configuration contract;
#: matches ``Settings.interpreter_api_key`` under ``env_prefix="COPILOT_"``.
API_KEY_ENV = "COPILOT_INTERPRETER_API_KEY"

#: Classification with structured output — the cheapest tier that does this well.
_MODEL = "claude-opus-5"

#: The reply is four short fields; 256 leaves room for a long quoted span without
#: paying for a model that decided to explain itself.
_MAX_TOKENS = 256

#: List price per million tokens for :data:`_MODEL`. Only used to log an estimate;
#: re-check against the published pricing table when the model changes.
_USD_PER_MTOK_IN = 5.00
_USD_PER_MTOK_OUT = 25.00

#: Long descriptions are mostly boilerplate (benefits, EEO statements) and the
#: level signal is near the top. Capping the tail bounds the worst-case bill;
#: verification still runs against the *full* description, so a span from the
#: truncated prefix is checked against everything.
_MAX_DESCRIPTION_CHARS = 12_000

#: Mirrors ``domain.seniority``'s own plausibility ceiling: a JD asking for 40
#: years is a parsing artefact, and so is a model that reports one.
_MAX_PLAUSIBLE_YEARS = 25

#: Guards against a model that "quotes" a single word (unverifiable in any useful
#: sense — "experience" appears everywhere) or pastes half the posting back.
_MIN_EVIDENCE_CHARS = 5
_MAX_EVIDENCE_CHARS = 400

_SYSTEM = (
    "You decide the seniority band of a software engineering job posting from its "
    "description.\n\n"
    "Bands:\n"
    "  intern  - an internship or co-op\n"
    "  entry    - new grad or junior; up to 2 years of professional experience expected\n"
    "  mid      - 3 to 5 years expected\n"
    "  senior   - 6+ years, or lead/staff/principal scope\n"
    "  unknown  - the description genuinely does not indicate a level\n\n"
    "Rules:\n"
    "- Judge what the posting REQUIRES, not what it prefers. "
    '"2+ years required, 5+ preferred" is entry.\n'
    '- Education is not experience. Ignore "4-year degree" and "graduating in 2026".\n'
    "- min_years is the smallest stated requirement, or null if none is stated.\n"
    "- evidence must be copied character-for-character from the description and must be "
    "the span that decided the band. Never paraphrase, never summarise, never quote the "
    "title. If no span in the description indicates a level, answer unknown with an "
    "empty evidence string.\n"
    "- confidence: high when an explicit requirement is stated, medium when the level is "
    "implied by scope or responsibilities, low when you are guessing.\n"
    "- Answer unknown rather than guessing a band you cannot quote."
)

#: Structured output, so parsing is not a failure surface. ``additionalProperties:
#: false`` plus a full ``required`` list is what the API enforces the schema on.
#: Nullable ``min_years`` is spelled with ``anyOf`` because numeric range keywords
#: (``minimum``/``maximum``) are not supported here — the range is clamped in code.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "band": {
            "type": "string",
            "enum": ["intern", "entry", "mid", "senior", "unknown"],
        },
        "min_years": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "evidence": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["band", "min_years", "evidence", "confidence"],
    "additionalProperties": False,
}


@dataclass
class InterpreterStats:
    """Running counters, so a run can report what it spent and what it caught."""

    cache_hits: int = 0
    calls: int = 0
    failures: int = 0
    unverified: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def estimated_cost_usd(self) -> float:
        """List-price estimate for the calls made. An estimate, not an invoice."""
        return (
            self.input_tokens / 1_000_000 * _USD_PER_MTOK_IN
            + self.output_tokens / 1_000_000 * _USD_PER_MTOK_OUT
        )


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _locate(description: str, span: str) -> tuple[bool, bool]:
    """``(found, verbatim)`` for ``span`` inside ``description``.

    Two tiers because HTML-derived descriptions carry hard line wraps: a model
    that re-flows ``"3 years'\\n  experience"`` onto one line has still pointed at
    a real span, but it has not quoted verbatim. Found-but-reflowed is accepted
    with a confidence downgrade; not found at all is a fabrication.
    """
    if span in description:
        return True, True
    collapsed = _collapse(span)
    if collapsed and collapsed in _collapse(description):
        return True, False
    return False, False


def verify_interpretation(parsed: Interpretation, description: str) -> Interpretation:
    """Confirm the span exists and the band agrees with the years given (pure).

    ``evidence_verified`` is true only when a non-empty span was located in the
    description. A band asserted without a locatable span is forced to ``LOW`` — it
    may still be right, but nothing in the posting backs it. ``UNKNOWN`` is exempt:
    it asserts nothing, so it needs no support.

    Public and pure because there are two routes to an interpretation — the Messages
    API and a Claude Code batch — and a model's answer must clear the same bar
    whichever produced it. This is the only place that decides what "verified" means.
    """
    found, verbatim = (
        _locate(description, parsed.evidence) if parsed.evidence else (False, False)
    )
    confidence = parsed.confidence

    if found and not verbatim:
        confidence = confidence.downgraded()
    elif not found and parsed.band is not Level.UNKNOWN:
        confidence = Confidence.LOW

    # Free self-consistency check against the rule tier's own mapping: if the model
    # reports 7 years and calls it entry, one of the two is wrong.
    if (
        parsed.min_years is not None
        and parsed.band is not Level.UNKNOWN
        and level_from_years(parsed.min_years) is not parsed.band
    ):
        confidence = confidence.downgraded()

    return Interpretation(
        band=parsed.band,
        min_years=parsed.min_years,
        evidence=parsed.evidence,
        confidence=confidence,
        evidence_verified=found,
    )


def _is_auth_failure(exc: Exception) -> bool:
    """Whether an exception means the credential itself is rejected.

    Matched on class name rather than by importing the SDK's exception types: the SDK
    is imported lazily so the package works without it installed, and importing it
    here to catch it would undo that. The names are stable public API
    (``AuthenticationError`` is 401, ``PermissionDeniedError`` is 403).

    Deliberately narrow. A 429 or a 529 is transient and the *next* posting may well
    succeed, so those must not trip the breaker — the failure this guards against is
    the one that is identical for every posting in the batch.
    """
    return type(exc).__name__ in _AUTH_FAILURE_TYPES


#: Rejected-credential exception names. 401 and 403 only.
_AUTH_FAILURE_TYPES = frozenset({"AuthenticationError", "PermissionDeniedError"})


class ClaudeInterpreter:
    """InterpreterPort backed by a hosted LLM, cached by posting id.

    The SDK is imported lazily inside :meth:`_get_client`, so importing this
    module costs nothing and needs no credentials. Pass ``client`` to drive it
    with a fake — that is how the tests run with no network and no key.

    The key is either handed in (``api_key``) or resolved through ``secrets`` from
    the SSM parameter named by ``secret_id``; direct wins. The lookup is lazy in a
    way that matters: it happens on the first posting that actually needs a model
    call, so a run whose postings are all cached — the steady state, since a
    description is read once ever — resolves no credential at all.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = _MODEL,
        store: PostingStorePort | None = None,
        client: Any | None = None,
        max_description_chars: int = _MAX_DESCRIPTION_CHARS,
        secrets: SecretsPort | None = None,
        secret_id: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model
        #: Set once a 401/403 proves the credential is rejected, so the rest of the
        #: batch is skipped instead of re-proving it per posting.
        self._auth_failed = False
        self._store = store
        self._client = client
        self._max_description_chars = max_description_chars
        self._stats = InterpreterStats()
        self._secrets = secrets
        self._secret_id = secret_id
        self._key_resolved = bool(api_key)

    @property
    def stats(self) -> InterpreterStats:
        return self._stats

    # --- port ----------------------------------------------------------------

    def interpret(self, posting: Posting) -> Interpretation | None:
        """Level for one posting, or ``None`` when this tier has nothing to add.

        Cache first, and *before* the credential check: an interpretation stored
        on a previous run stays usable on a run with no key at all.
        """
        cached = self._cached(posting.id)
        if cached is not None:
            self._stats.cache_hits += 1
            return cached
        fresh = self._ask(posting)
        if fresh is None:
            return None
        self._save(posting.id, fresh)
        return fresh

    def interpret_many(self, postings: Sequence[Posting]) -> dict[str, Interpretation]:
        """Levels keyed by posting id, omitting the ones with no answer.

        A sequential loop on purpose: after the first run the overwhelming
        majority of these are cache hits, so there is nothing to parallelise, and
        a per-posting call means one bad posting cannot take down the batch.
        """
        results: dict[str, Interpretation] = {}
        for posting in postings:
            verdict = self.interpret(posting)
            if verdict is not None:
                results[posting.id] = verdict
        _LOG.info(
            "interpret_batch",
            extra={
                "extra_fields": {
                    "postings": len(postings),
                    "resolved": len(results),
                    "cache_hits": self._stats.cache_hits,
                    "calls": self._stats.calls,
                    "failures": self._stats.failures,
                    "unverified": self._stats.unverified,
                    "est_usd": round(self._stats.estimated_cost_usd(), 4),
                }
            },
        )
        return results

    # --- cache ---------------------------------------------------------------

    def _cached(self, posting_id: str) -> Interpretation | None:
        if self._store is None:
            return None
        return Interpretation.from_payload(self._store.cached_interpretation(posting_id))

    def _save(self, posting_id: str, verdict: Interpretation) -> None:
        if self._store is not None:
            self._store.save_interpretation(posting_id, verdict.to_payload())

    # --- model call ----------------------------------------------------------

    def _ask(self, posting: Posting) -> Interpretation | None:
        description = posting.description or ""
        if not posting.desc_available or not description.strip():
            # No description, nothing to read. Spending a call here would buy a
            # confident answer derived from the title alone, which the rule tier
            # has already decided better.
            return None
        client = self._get_client()
        if client is None:
            return None
        if self._auth_failed:
            # Set by a previous 401/403 in this same batch. See _auth_failed.
            return None

        self._stats.calls += 1
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": self.build_prompt(posting, self._max_description_chars),
                    }
                ],
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
        except Exception as exc:
            # Deliberately broad. The SDK already retries 408/409/429/5xx twice,
            # so anything arriving here is terminal for this posting, and a daily
            # cron over hundreds of postings must not abort because one of them
            # failed. The exception type is logged so a pattern is still visible.
            self._stats.failures += 1
            _LOG.warning(
                "interpret_call_failed",
                extra={
                    "extra_fields": {
                        "posting": posting.id,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:200],
                    }
                },
            )
            if _is_auth_failure(exc):
                # A rejected credential is not a per-posting problem, and retrying it
                # 631 more times only spends the cron's remaining seconds discovering
                # the same 401. Measured before this existed: 12 postings produced 12
                # failed calls, so the real corpus would produce 631 — on a run that
                # already uses 786 s of its 900 s budget.
                #
                # Scoped to the instance, not the process: the next run builds a new
                # interpreter and re-reads the parameter, so fixing the key takes
                # effect on the next sweep with nothing to reset by hand.
                self._auth_failed = True
                _LOG.error(
                    "interpret_auth_failed_giving_up",
                    extra={
                        "extra_fields": {
                            "parameter": self._secret_id,
                            "remaining_skipped": True,
                            "hint": (
                                "the credential was rejected; a claude setup-token "
                                "(sk-ant-oat01-) cannot call the Messages API — that "
                                "needs a Console key (sk-ant-api03-)"
                            ),
                        }
                    },
                )
            return None

        self._record_usage(response)
        parsed = self._parse(response, posting.id)
        if parsed is None:
            return None
        return self._verify(parsed, description, posting.id)

    def _resolved_key(self) -> str:
        """The API key to use, or ``""``. Resolved at most once per instance.

        Absence is cached as firmly as presence. ``interpret_many`` walks hundreds
        of postings, and a run with no key configured must make zero credential
        calls after the first, not one per uncached posting.
        """
        if self._key_resolved or self._secrets is None:
            return self._api_key
        self._api_key = self._secrets.api_key(
            parameter_name=self._secret_id, env_var=API_KEY_ENV
        )
        self._key_resolved = True
        return self._api_key

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        api_key = self._resolved_key()
        if not api_key:
            return None
        try:
            sdk = import_module("anthropic")
        except ImportError:
            # The Lambda bundle is assembled by hand; a missing wheel is a real
            # deployment outcome and must degrade exactly like a missing key.
            _LOG.warning("interpret_sdk_missing")
            return None
        self._client = sdk.Anthropic(api_key=api_key)
        return self._client

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self._stats.input_tokens += _as_int(getattr(usage, "input_tokens", 0))
        self._stats.output_tokens += _as_int(getattr(usage, "output_tokens", 0))

    # --- parsing and checking ------------------------------------------------

    def _parse(self, response: Any, posting_id: str) -> Interpretation | None:
        """Structured reply -> Interpretation, or ``None`` if it is not usable.

        The schema makes well-formed JSON the norm, not a guarantee: a truncated
        reply, a model that does not honour ``output_config``, or a transport that
        returns an empty content list all land here, and all of them must be a
        quiet ``None`` rather than an exception in the middle of a batch.
        """
        if getattr(response, "stop_reason", None) == "max_tokens":
            self._note_malformed(posting_id, "truncated")
            return None
        raw = _first_text(response)
        if not raw:
            self._note_malformed(posting_id, "no_text_block")
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            self._note_malformed(posting_id, "not_json")
            return None
        if not isinstance(data, dict):
            self._note_malformed(posting_id, "not_object")
            return None
        try:
            band = Level(data["band"])
            confidence = Confidence(data["confidence"])
        except (KeyError, TypeError, ValueError):
            self._note_malformed(posting_id, "bad_enum")
            return None
        evidence = data.get("evidence")
        return Interpretation(
            band=band,
            min_years=_clean_years(data.get("min_years")),
            evidence=_clean_evidence(evidence) if isinstance(evidence, str) else "",
            confidence=confidence,
            evidence_verified=False,  # nothing has been checked yet
        )

    def _note_malformed(self, posting_id: str, reason: str) -> None:
        """Count and log an unreadable reply. Callers return ``None`` themselves."""
        self._stats.failures += 1
        _LOG.warning(
            "interpret_reply_malformed",
            extra={"extra_fields": {"posting": posting_id, "reason": reason}},
        )

    def _verify(self, parsed: Interpretation, description: str, posting_id: str) -> Interpretation:
        """Verify, and log/count the one case worth alerting on.

        The judgement itself lives in :func:`verify_interpretation` so the batch
        route in ``scripts/level_batch.py`` applies exactly the same rules. Two
        implementations of "is this evidence real" would eventually disagree, and the
        disagreement would show up as a posting that one route trusts and the other
        does not.
        """
        checked = verify_interpretation(parsed, description)
        if not checked.evidence_verified and parsed.band is not Level.UNKNOWN:
            self._stats.unverified += 1
            _LOG.warning(
                "interpret_evidence_unverified",
                extra={
                    "extra_fields": {
                        "posting": posting_id,
                        "band": parsed.band.value,
                        "span": parsed.evidence[:120],
                    }
                },
            )
        return checked

    # --- prompt --------------------------------------------------------------

    @staticmethod
    def build_prompt(posting: Posting, max_description_chars: int = _MAX_DESCRIPTION_CHARS) -> str:
        """Assemble the user turn. Pure and unit-tested.

        The title is included as context even though the rule tier already read
        it — "Software Engineer, Platform Infrastructure" is a hint the
        description often assumes you have. The system prompt forbids quoting it,
        and :meth:`_verify` catches a span taken from it anyway, because the title
        is not part of the text the span is checked against.
        """
        description = (posting.description or "").strip()
        if len(description) > max_description_chars:
            description = description[:max_description_chars]
        return (
            f"Title: {posting.title}\n"
            f"Company: {posting.company}\n\n"
            "Description:\n"
            f"{description}"
        )


def _first_text(response: Any) -> str:
    """Text of the first text block, or ``""``. Tolerant of any block ordering."""
    for block in getattr(response, "content", None) or ():
        if getattr(block, "type", "") == "text":
            return str(getattr(block, "text", "") or "")
    return ""


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _clean_years(value: Any) -> int | None:
    """Keep only a plausible integer. ``bool`` is an ``int`` in Python — exclude it."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 <= value <= _MAX_PLAUSIBLE_YEARS else None


def _clean_evidence(value: str) -> str:
    span = value.strip()
    if len(span) < _MIN_EVIDENCE_CHARS:
        return ""
    return span[:_MAX_EVIDENCE_CHARS]
