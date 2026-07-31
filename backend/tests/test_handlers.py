"""Handler tests wired with in-memory fakes — no cloud, no network."""
from __future__ import annotations

import json
from typing import Any

from conftest import (
    FakeLLM,
    FakeMailbox,
    FakePostings,
    FakeStore,
    make_briefing,
    make_posting,
)

from copilot.adapters.gmail_mailbox import GmailMailbox
from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.adapters.ssm_secrets import AwsSecrets
from copilot.config import Settings
from copilot.domain.models import Email
from copilot.handlers import api, cron
from copilot.services.daily_briefing import DailyBriefingService

RESUME = "Python, REST, AWS, Lambda, DynamoDB, React, TypeScript, pytest, CI/CD"


def _service(
    mailbox: FakeMailbox, postings: FakePostings, store: FakeStore
) -> DailyBriefingService:
    return DailyBriefingService(
        mailbox=mailbox,
        store=store,
        postings=postings,
        posting_store=SqlitePostingStore(":memory:"),
        llm=FakeLLM(),
        resume_text=RESUME,
    )


def test_cron_run_briefing_and_response() -> None:
    mailbox = FakeMailbox(
        [
            Email(
                sender="Ashby <no-reply@ashbyhq.com>",
                subject="Interview invitation",
                snippet="can we schedule a call?",
            ),
            Email(sender="deals@shop.com", subject="50% off", snippet="sale"),
        ]
    )
    postings = FakePostings([make_posting(1, "New Grad Software Engineer")])
    store = FakeStore()
    settings = Settings(owner_user_id="u1", my_email="me@example.com")

    run = cron.run_briefing(_service(mailbox, postings, store), settings)
    response = cron.briefing_response(run)

    # The inbox half is unchanged; the supply half is new and its counts are what
    # make a shrinking corpus impossible to miss.
    assert response["ok"] is True
    assert response["scanned"] == 2
    assert response["needs_action"] == 1
    assert response["fetched"] == 1
    assert response["new"] == 1
    assert response["kept"] == 1
    assert response["scored"] is True
    # persisted under the owner + self-briefing email sent, replies only drafted
    assert store.latest_briefing("u1") is run.briefing
    assert len(mailbox.drafts) == 1
    assert mailbox.sent[0][0] == "me@example.com"


class TestTheRunSummaryReportsTheScreeningView:
    """A screen that silently produced nothing must be as visible as a fetch that did.

    The public read API now serves a view the cron writes, so "green cron, blank
    website" is a reachable state — the 2026 restaging of the fixture bug, where a
    job source that existed on no machine served four invented companies and every
    count downstream looked fine.
    """

    def _response(self, *titles: str) -> dict[str, Any]:
        postings = [make_posting(n, title) for n, title in enumerate(titles, start=1)]
        run = cron.run_briefing(
            _service(FakeMailbox([]), FakePostings(postings), FakeStore()),
            Settings(owner_user_id="u1"),
        )
        return cron.briefing_response(run)

    def test_the_view_counts_are_unconditional(self) -> None:
        """A metric filter on ``eligible`` must not have to guess whether the key is
        there; a conditional count is how a shrinking corpus stays invisible."""
        response = self._response(
            "New Grad Software Engineer", "Senior Staff Engineer", "Marketing Intern"
        )
        assert response["screened"] == 3
        assert response["eligible"] == 1
        assert response["internships"] == 0
        assert response["view_rows"] >= 1

    def test_screened_and_fetched_are_both_reported(self) -> None:
        """They are different numbers — ``screened`` is the open corpus, ``fetched`` is
        what the boards returned this morning — and a run where they diverge is worth
        looking at, which is only possible if both are present."""
        response = self._response("New Grad Software Engineer")
        assert response["fetched"] == 1
        assert response["screened"] == 1

    def test_the_internships_collection_is_reported_separately(self) -> None:
        """48 software internships against 318 internship-gate fires on the live
        corpus. Reporting only the gate count would overstate the section 6-fold."""
        response = self._response("Software Engineer Intern", "Marketing Intern")
        assert response["internships"] == 1
        assert response["eligible"] == 0

    def test_a_healthy_run_carries_no_skip_key(self) -> None:
        response = self._response("New Grad Software Engineer")
        assert "screen_skipped" not in response
        assert "close_skipped" not in response

    def test_an_empty_sweep_still_reports_every_view_count(self) -> None:
        """``screened: 0`` with ``fetched: 0`` is a broken sweep; the same zero with a
        non-zero ``fetched`` would be a broken screen. Both have to be sayable."""
        response = self._response()
        assert response["fetched"] == 0
        assert response["screened"] == 0
        assert response["eligible"] == 0
        assert response["view_rows"] == 0
        assert response["close_skipped"] == "empty_fetch"


def test_cron_build_service_wires_real_adapters() -> None:
    service = cron.build_service(Settings())
    # mypy already proves the port contracts; assert the wiring at runtime too.
    assert isinstance(service, DailyBriefingService)


class TestTheCredentialPortIsActuallyWired:
    """The bug: a granted permission nothing exercises looks like a working feature.

    ``adapters/ssm_secrets.py`` shipped with its own tests passing, the stack granted
    the cron ``ssm:GetParameter`` on both key paths and set ``COPILOT_LLM_SECRET_ID``
    and ``COPILOT_INTERPRETER_SECRET_ID`` to them — and ``build_service`` still
    constructed every consumer without a resolver. So no key could ever be read, the
    inbox could only ever report ``inbox_ok: false``, no reply could ever be drafted,
    and nothing anywhere failed: the IAM policy was right, the env vars were right,
    the adapter was right, and the runtime never called it.

    The old test asserted ``isinstance(service, DailyBriefingService)``, which was
    true throughout. These assert the edges that carry credentials.
    """

    def test_the_mailbox_can_reach_the_secret_store(self) -> None:
        service = cron.build_service(Settings())
        assert isinstance(service.mailbox, GmailMailbox)
        assert service.mailbox._secrets is not None
        assert service.mailbox._secret_id == Settings().gmail_secret_id

    def test_the_reply_drafter_can_reach_the_secret_store(self) -> None:
        service = cron.build_service(Settings())
        assert isinstance(service.llm, LlmReplyDrafter)
        assert service.llm._secrets is not None
        assert service.llm._secret_id == Settings().llm_secret_id

    def test_one_resolver_is_shared_across_the_invocation(self) -> None:
        """Per-invocation, not per-adapter and not module-level.

        Per-adapter would pay a second SSM read for the same parameter. Module-level
        would outlive a key rotation: the resolver caches what it reads, and a warm
        container lives for hours, so a rotated key would keep being the old one until
        the container died.
        """
        service = cron.build_service(Settings())
        assert isinstance(service.mailbox, GmailMailbox)
        assert isinstance(service.llm, LlmReplyDrafter)
        assert service.mailbox._secrets is service.llm._secrets

    def test_a_second_invocation_builds_a_fresh_resolver(self) -> None:
        """The other half of the rotation argument: nothing is cached across runs."""
        first = cron.build_service(Settings())
        second = cron.build_service(Settings())
        assert isinstance(first.mailbox, GmailMailbox)
        assert isinstance(second.mailbox, GmailMailbox)
        assert first.mailbox._secrets is not second.mailbox._secrets

    def test_the_secret_ids_are_the_paths_the_stack_creates(self) -> None:
        """Defaults that disagree with the deployed env vars teach the wrong name.

        These two used to default to ``career-copilot/llm`` and
        ``career-copilot/interpreter`` — Secrets Manager-shaped names. The stack
        overrides them with SSM *paths*, so the cloud was correct and every local run
        looked up a parameter that will never exist. Pinned against
        ``infra/lib/career-copilot-stack.ts``; changing one side fails here.
        """
        settings = Settings()
        assert settings.llm_secret_id == "/career-copilot/llm-api-key"
        assert settings.interpreter_secret_id == "/career-copilot/interpreter-api-key"
        # Gmail is the one that really is a Secrets Manager id: a JSON document.
        assert settings.gmail_secret_id == "career-copilot/gmail"
        assert not settings.gmail_secret_id.startswith("/")

    def test_building_the_service_reads_no_credential(self) -> None:
        """Wiring must stay lazy, or a keyless run pays for lookups it never uses.

        ``build_service`` runs on every invocation including ones that never touch the
        inbox; resolving both keys eagerly there would add two SSM round trips to a
        cron whose supply half needs none.
        """
        settings = Settings()
        service = cron.build_service(settings)
        assert isinstance(service.mailbox, GmailMailbox)
        assert isinstance(service.llm, LlmReplyDrafter)
        resolver = service.mailbox._secrets
        assert isinstance(resolver, AwsSecrets)
        assert resolver._keys == {}
        assert resolver._documents == {}


def _event(sub: str | None, *, rest_shape: bool = False) -> dict[str, Any]:
    if sub is None:
        return {"requestContext": {"authorizer": {}}}
    claims = {"sub": sub}
    authorizer = {"claims": claims} if rest_shape else {"jwt": {"claims": claims}}
    return {"requestContext": {"authorizer": authorizer}}


def test_api_returns_latest_briefing_for_jwt_subject() -> None:
    store = FakeStore()
    briefing = make_briefing()
    store.save_briefing("user-123", briefing)

    resp = api.read_latest(store, _event("user-123"))

    assert resp["statusCode"] == 200
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    body = json.loads(resp["body"])
    assert body["briefing"]["day"] == briefing.day.isoformat()
    assert body["markdown"].startswith("# ")


def test_api_supports_rest_authorizer_shape() -> None:
    store = FakeStore()
    store.save_briefing("user-123", make_briefing())
    resp = api.read_latest(store, _event("user-123", rest_shape=True))
    assert resp["statusCode"] == 200


def test_api_401_without_subject() -> None:
    resp = api.read_latest(FakeStore(), _event(None))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}


def test_api_404_when_no_briefing_yet() -> None:
    resp = api.read_latest(FakeStore(), _event("nobody"))
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"]) == {"error": "no_briefing_yet"}


def test_user_id_from_event_none_when_missing() -> None:
    assert api.user_id_from_event({}) is None
