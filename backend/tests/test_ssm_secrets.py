"""Unit tests for the credential path: the resolver and the three consumers.

No network, no moto, no AWS credentials — a fake boto3 client and an in-memory
:class:`SecretsPort` double. Every test names the failure it prevents, because the
defect this code fixes was not a wrong behaviour but an *absent* one: the stack has
been granting reads on a secret and two SSM parameters that no line of Python ever
asked for, so the deployed inbox half has reported ``inbox_ok: false`` every day
and the level interpreter has run in no pipeline at all.

The single most important property under test is the boring one: **a credential
lookup can fail in six different ways and none of them may raise.** Job supply is
five unauthenticated ATS APIs and regex gates, so an exception on this path would
take down 25k postings of working, keyless product to protect a tier that is
optional by design.
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from copilot.adapters import claude_interpreter as interp_mod
from copilot.adapters import gmail_mailbox as gmail_mod
from copilot.adapters import llm_reply as llm_mod
from copilot.adapters.claude_interpreter import ClaudeInterpreter
from copilot.adapters.gmail_mailbox import (
    GmailMailbox,
    load_gmail_credentials,
    missing_gmail_fields,
)
from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.adapters.ssm_secrets import AwsSecrets
from copilot.domain.models import Email
from copilot.domain.posting import Posting
from copilot.logging import JsonFormatter
from copilot.ports.secrets import SecretsPort

#: The real names the CDK stack passes in ``COPILOT_*_SECRET_ID``. Spelled out so
#: a rename on either side shows up as a diff in a test, not as a silent absence
#: in production — which is precisely the failure mode this module exists to end.
LLM_PARAM = "/career-copilot/llm-api-key"
INTERPRETER_PARAM = "/career-copilot/interpreter-api-key"
GMAIL_SECRET = "career-copilot/gmail"

#: Distinctive on purpose: every log assertion below greps for it, including for a
#: truncated prefix of it.
_KEY = "sk-live-never-log-me-9f3a"

_GOOD_GMAIL = {
    "refresh_token": "1//refresh",
    "client_id": "123.apps.googleusercontent.com",
    "client_secret": "shh",
}


# --- fakes -------------------------------------------------------------------


class _AwsError(Exception):
    """A botocore ``ClientError`` lookalike: the code lives in ``.response``.

    Not the real class: constructing one needs botocore at module scope, which is
    the lazy import the adapter is careful to avoid, and the adapter reads the code
    out of the response mapping precisely so it can be doubled like this.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"{code}: refused")
        self.response = {"Error": {"Code": code, "Message": "refused"}}


class _FakeSsm:
    """Stand-in for ``boto3.client("ssm")``."""

    def __init__(
        self,
        parameters: Mapping[str, str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._parameters = dict(parameters or {})
        self._error = error
        self.calls: list[dict[str, Any]] = []

    # AWS-cased keyword arguments, because the point of this double is that the
    # adapter calls the real API shape.
    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        if self._error is not None:
            raise self._error
        if Name not in self._parameters:
            raise _AwsError("ParameterNotFound")
        return {
            "Parameter": {
                "Name": Name,
                "Type": "SecureString",
                "Value": self._parameters[Name],
            }
        }


class _FakeSecretsManager:
    """Stand-in for ``boto3.client("secretsmanager")``."""

    def __init__(
        self,
        secrets: Mapping[str, str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._secrets = dict(secrets or {})
        self._error = error
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if self._error is not None:
            raise self._error
        if SecretId not in self._secrets:
            raise _AwsError("ResourceNotFoundException")
        return {"Name": SecretId, "SecretString": self._secrets[SecretId]}


class _FakeSecrets:
    """In-memory SecretsPort for the consumer tests.

    It counts lookups, because "how many times did we ask" is a behaviour with a
    network round trip and a per-invocation bill attached, not an implementation
    detail.
    """

    def __init__(
        self,
        keys: Mapping[str, str] | None = None,
        documents: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._keys = dict(keys or {})
        self._documents = dict(documents or {})
        self.key_calls: list[tuple[str, str]] = []
        self.document_calls: list[str] = []

    def api_key(self, *, parameter_name: str, env_var: str = "") -> str:
        self.key_calls.append((parameter_name, env_var))
        return self._keys.get(parameter_name, "")

    def secret_json(self, secret_id: str) -> Mapping[str, str] | None:
        self.document_calls.append(secret_id)
        return self._documents.get(secret_id)


@contextmanager
def _captured(logger_name: str) -> Iterator[list[str]]:
    """Formatted log lines from one adapter logger.

    ``copilot.logging`` sets ``propagate = False`` on purpose, so pytest's
    ``caplog`` — which hooks the *root* logger — sees nothing from these loggers
    and a leak assertion written with it would pass vacuously. Capturing on the
    logger itself and through the real :class:`JsonFormatter` also tests the thing
    that actually matters: the exact text that lands in CloudWatch.
    """
    handler = logging.Handler()
    lines: list[str] = []
    formatter = JsonFormatter()

    def emit(record: logging.LogRecord) -> None:
        lines.append(formatter.format(record))

    handler.emit = emit  # type: ignore[method-assign]
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)


# --- the port contract -------------------------------------------------------


def test_adapter_and_double_both_satisfy_the_port() -> None:
    """A double that does not type-check against its port proves less than it looks.

    Both assignments are checked by mypy, so a signature drift between
    ``SecretsPort`` and either implementation fails the type gate rather than
    surfacing as an AttributeError inside a Lambda.
    """
    real: SecretsPort = AwsSecrets(ssm=_FakeSsm())
    fake: SecretsPort = _FakeSecrets()

    assert real.api_key(parameter_name="") == ""
    assert fake.secret_json("") is None


# --- SSM: the happy path -----------------------------------------------------


def test_present_parameter_resolves_and_is_read_decrypted() -> None:
    """Without ``WithDecryption`` SSM hands back ciphertext, which "works" and 401s."""
    ssm = _FakeSsm({LLM_PARAM: _KEY})

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == _KEY
    assert ssm.calls == [{"Name": LLM_PARAM, "WithDecryption": True}]


def test_trailing_newline_is_stripped() -> None:
    """``put-parameter --value "$(cat key.txt)"`` stores the newline too.

    A newline inside an Authorization header fails as a malformed request, not as
    a bad key — an hour of debugging the wrong layer.
    """
    ssm = _FakeSsm({LLM_PARAM: f"  {_KEY}\n"})

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == _KEY


# --- SSM: every way absence arrives ------------------------------------------


def test_absent_parameter_is_no_credential_not_an_error() -> None:
    """The steady state today: neither optional key exists, and the sweep must run."""
    assert AwsSecrets(ssm=_FakeSsm()).api_key(parameter_name=LLM_PARAM) == ""


def test_empty_parameter_value_is_no_credential() -> None:
    """A parameter created by hand with whitespace must not read as a key."""
    ssm = _FakeSsm({LLM_PARAM: "   \n"})

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == ""


def test_access_denied_is_no_credential_not_an_error() -> None:
    """A missing or mis-scoped IAM grant must not take down a keyless job sweep."""
    ssm = _FakeSsm(error=_AwsError("AccessDeniedException"))

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == ""


def test_throttling_is_no_credential_not_an_error() -> None:
    """Retries are botocore's job; when they run out the tier degrades, not the run."""
    ssm = _FakeSsm(error=_AwsError("ThrottlingException"))

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == ""


def test_transport_error_with_no_response_is_no_credential() -> None:
    """``EndpointConnectionError``/``NoCredentialsError`` carry no ``.response``.

    Reading the error code must fall back to the class name instead of raising an
    AttributeError on the degradation path itself.
    """
    ssm = _FakeSsm(error=RuntimeError("Could not connect to the endpoint URL"))

    assert AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM) == ""


def test_no_parameter_name_makes_no_call() -> None:
    """The local default: ``Settings`` ships an id, but a script may pass none."""
    ssm = _FakeSsm({LLM_PARAM: _KEY})

    assert AwsSecrets(ssm=ssm).api_key(parameter_name="") == ""
    assert ssm.calls == []


def test_missing_boto3_is_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Lambda bundle is assembled by hand; a missing wheel must not crash it.

    ``None`` in ``sys.modules`` is what makes ``import boto3`` raise ImportError
    without uninstalling anything.
    """
    monkeypatch.setitem(sys.modules, "boto3", None)

    assert AwsSecrets().api_key(parameter_name=LLM_PARAM) == ""


# --- precedence --------------------------------------------------------------


def test_direct_env_var_wins_over_the_parameter_store() -> None:
    """What makes local dev and the test suite work with no AWS account at all.

    The store must not even be consulted: reproducing a cloud problem on a laptop
    cannot require read access to the production credential.
    """
    ssm = _FakeSsm({LLM_PARAM: "from-ssm"})
    resolver = AwsSecrets(ssm=ssm, env={"COPILOT_LLM_API_KEY": "from-env"})

    resolved = resolver.api_key(parameter_name=LLM_PARAM, env_var="COPILOT_LLM_API_KEY")

    assert resolved == "from-env"
    assert ssm.calls == []


def test_blank_env_var_falls_through_to_the_parameter_store() -> None:
    """``COPILOT_LLM_API_KEY=""`` is how a shell exports "unset", not "no key".

    Lambda env vars and ``.env`` files both produce empty strings routinely; an
    empty override that shadowed a real parameter would disable the tier that was
    correctly configured.
    """
    ssm = _FakeSsm({LLM_PARAM: _KEY})
    resolver = AwsSecrets(ssm=ssm, env={"COPILOT_LLM_API_KEY": "  "})

    assert resolver.api_key(parameter_name=LLM_PARAM, env_var="COPILOT_LLM_API_KEY") == _KEY


# --- caching -----------------------------------------------------------------


def test_second_lookup_of_the_same_name_hits_the_cache() -> None:
    """One cron resolves several credentials; ``GetParameter`` is a network call."""
    ssm = _FakeSsm({LLM_PARAM: _KEY})
    resolver = AwsSecrets(ssm=ssm)

    assert resolver.api_key(parameter_name=LLM_PARAM) == _KEY
    assert resolver.api_key(parameter_name=LLM_PARAM) == _KEY
    assert len(ssm.calls) == 1


def test_absence_is_cached_too() -> None:
    """Re-asking for a parameter that does not exist wastes the same round trip.

    Absence is the *expected* answer for both optional keys, so this is the path
    that would repeat, not the exception.
    """
    ssm = _FakeSsm()
    resolver = AwsSecrets(ssm=ssm)

    assert resolver.api_key(parameter_name=INTERPRETER_PARAM) == ""
    assert resolver.api_key(parameter_name=INTERPRETER_PARAM) == ""
    assert len(ssm.calls) == 1


def test_two_names_are_cached_independently() -> None:
    """One resolver serves both keys; a single-slot cache would cross them."""
    ssm = _FakeSsm({LLM_PARAM: "llm-key", INTERPRETER_PARAM: "interp-key"})
    resolver = AwsSecrets(ssm=ssm)

    assert resolver.api_key(parameter_name=LLM_PARAM) == "llm-key"
    assert resolver.api_key(parameter_name=INTERPRETER_PARAM) == "interp-key"


def test_a_fresh_resolver_looks_the_credential_up_again() -> None:
    """The cache is per instance, so a rotated key takes effect on the next run.

    A module-level cache would live for the whole warm Lambda container — hours —
    and keep serving a key that had since been rotated or revoked.
    """
    ssm = _FakeSsm({LLM_PARAM: _KEY})

    AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM)
    AwsSecrets(ssm=ssm).api_key(parameter_name=LLM_PARAM)

    assert len(ssm.calls) == 2


# --- logging: names only -----------------------------------------------------


def test_successful_resolution_logs_the_name_and_nothing_of_the_value() -> None:
    """Not the value, not its length, not a prefix, not a masked form.

    A length narrows a brute-force search and a prefix identifies the provider and
    key type, and anything that reaches CloudWatch cannot be un-leaked from it.
    """
    resolver = AwsSecrets(ssm=_FakeSsm({LLM_PARAM: _KEY}))

    with _captured("copilot.adapters.ssm_secrets") as lines:
        assert resolver.api_key(parameter_name=LLM_PARAM) == _KEY

    joined = "\n".join(lines)
    assert LLM_PARAM in joined
    assert _KEY not in joined
    assert "sk-live" not in joined  # no prefix, no masked form
    assert str(len(_KEY)) not in joined


def test_failed_resolution_logs_the_name_and_the_aws_code() -> None:
    """The two facts that fix it, and nothing that identifies a credential."""
    resolver = AwsSecrets(ssm=_FakeSsm(error=_AwsError("AccessDeniedException")))

    with _captured("copilot.adapters.ssm_secrets") as lines:
        resolver.api_key(parameter_name=INTERPRETER_PARAM)

    joined = "\n".join(lines)
    assert INTERPRETER_PARAM in joined
    assert "AccessDeniedException" in joined
    assert "WARNING" in joined


def test_a_parameter_that_was_never_created_logs_at_info_not_warning() -> None:
    """An optional tier that is switched off is not an incident.

    If ``ParameterNotFound`` warned, every single daily run would log two warnings
    for a product working exactly as documented — and a real ``AccessDenied``
    would be invisible in the noise.
    """
    resolver = AwsSecrets(ssm=_FakeSsm())

    with _captured("copilot.adapters.ssm_secrets") as lines:
        resolver.api_key(parameter_name=LLM_PARAM)

    joined = "\n".join(lines)
    assert "WARNING" not in joined
    assert "secret_absent" in joined


# --- Secrets Manager ---------------------------------------------------------


def test_secret_json_returns_the_string_fields() -> None:
    manager = _FakeSecretsManager({GMAIL_SECRET: '{"refresh_token": "1//x", "client_id": "cid"}'})

    document = AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET)

    assert document == {"refresh_token": "1//x", "client_id": "cid"}
    assert manager.calls == [GMAIL_SECRET]


def test_absent_secret_is_none() -> None:
    assert AwsSecrets(secrets_manager=_FakeSecretsManager()).secret_json(GMAIL_SECRET) is None


def test_empty_secret_string_is_none() -> None:
    """The stack creates this secret and a human seeds it, so empty really happens."""
    manager = _FakeSecretsManager({GMAIL_SECRET: "   "})

    assert AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET) is None


def test_non_json_secret_is_none() -> None:
    """A token pasted raw instead of as a JSON document must not raise mid-run."""
    manager = _FakeSecretsManager({GMAIL_SECRET: "1//raw-refresh-token"})

    assert AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET) is None


def test_non_string_fields_are_dropped_not_passed_through() -> None:
    """``Mapping[str, str]`` has to be true, not merely annotated.

    A credential constructor handed a dict where it expects a token fails deep
    inside a vendor SDK, naming neither the field nor the secret.
    """
    manager = _FakeSecretsManager(
        {GMAIL_SECRET: '{"refresh_token": "1//x", "expiry": 1730000000, "nested": {"a": "b"}}'}
    )

    document = AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET)

    assert document == {"refresh_token": "1//x"}


def test_secret_with_no_usable_fields_is_none() -> None:
    manager = _FakeSecretsManager({GMAIL_SECRET: '{"expiry": 1730000000}'})

    assert AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET) is None


def test_secret_access_denied_is_none_not_an_error() -> None:
    manager = _FakeSecretsManager(error=_AwsError("AccessDeniedException"))

    assert AwsSecrets(secrets_manager=manager).secret_json(GMAIL_SECRET) is None


def test_secret_document_is_cached_including_its_absence() -> None:
    manager = _FakeSecretsManager()
    resolver = AwsSecrets(secrets_manager=manager)

    assert resolver.secret_json(GMAIL_SECRET) is None
    assert resolver.secret_json(GMAIL_SECRET) is None
    assert len(manager.calls) == 1


def test_secret_value_is_never_logged() -> None:
    manager = _FakeSecretsManager({GMAIL_SECRET: '{"refresh_token": "1//never-log-me"}'})
    resolver = AwsSecrets(secrets_manager=manager)

    with _captured("copilot.adapters.ssm_secrets") as lines:
        resolver.secret_json(GMAIL_SECRET)

    joined = "\n".join(lines)
    assert GMAIL_SECRET in joined
    assert "never-log-me" not in joined


# --- consumer: the reply drafter ---------------------------------------------


_EMAIL = Email(sender="recruiter@acme.com", subject="Interview", snippet="free next week?")


class _FakeSdkMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs: Any) -> Any:
        block = type("_Block", (), {"type": "text", "text": self._text})()
        return type("_Resp", (), {"content": [block]})()


class _FakeSdkClient:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.messages = _FakeSdkMessages("Happy to chat.")


def _fake_sdk_module(built: list[_FakeSdkClient]) -> Any:
    """A stand-in for ``anthropic`` that records the key it was constructed with.

    The entry point is ``Anthropic(api_key=...)``, not the previous vendor's
    ``Client(api_key=...)``. Named here because a fake with the old attribute made
    the adapter's own construction line unreachable while the test still passed.
    """

    def client(*, api_key: str) -> _FakeSdkClient:
        made = _FakeSdkClient(api_key=api_key)
        built.append(made)
        return made

    return type("_Sdk", (), {"Anthropic": staticmethod(client)})


def test_drafter_key_from_the_store_reaches_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this prevents: resolving into a local and constructing with the stale field.

    Asserting the drafted body alone would pass while the SDK was handed ``""``,
    so the test checks the key the client was actually built with.
    """
    built: list[_FakeSdkClient] = []
    monkeypatch.setattr(llm_mod, "import_module", lambda name: _fake_sdk_module(built))
    secrets = _FakeSecrets({LLM_PARAM: _KEY})
    drafter = LlmReplyDrafter(secrets=secrets, secret_id=LLM_PARAM)

    assert drafter.draft_reply(_EMAIL) == "Happy to chat."
    assert [c.api_key for c in built] == [_KEY]
    assert secrets.key_calls == [(LLM_PARAM, "COPILOT_LLM_API_KEY")]


def test_drafter_prefers_a_direct_key_and_never_asks_the_store() -> None:
    """Precedence, from the consumer's side: an injected key means no AWS call."""
    secrets = _FakeSecrets({LLM_PARAM: "from-ssm"})
    drafter = LlmReplyDrafter(api_key="from-caller", secrets=secrets, secret_id=LLM_PARAM)

    assert drafter._resolved_key() == "from-caller"
    assert secrets.key_calls == []


def test_drafter_with_nothing_configured_still_returns_no_draft() -> None:
    """The path that runs today: no key anywhere, and the daily run completes."""
    assert LlmReplyDrafter(secrets=_FakeSecrets(), secret_id=LLM_PARAM).draft_reply(_EMAIL) == ""


def test_drafter_asks_the_store_once_even_when_the_answer_is_no() -> None:
    """Drafting loops over every needs-action email; absence must not re-query."""
    secrets = _FakeSecrets()
    drafter = LlmReplyDrafter(secrets=secrets, secret_id=LLM_PARAM)

    drafter.draft_reply(_EMAIL)
    drafter.draft_reply(_EMAIL)

    assert len(secrets.key_calls) == 1


def test_drafter_missing_sdk_degrades_like_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newly reachable: before a key could be resolved in the cloud, this was dead code."""

    def _boom(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(llm_mod, "import_module", _boom)
    drafter = LlmReplyDrafter(secrets=_FakeSecrets({LLM_PARAM: _KEY}), secret_id=LLM_PARAM)

    assert drafter.draft_reply(_EMAIL) == ""


# --- consumer: the level interpreter -----------------------------------------


_POSTING = Posting(
    title="Software Engineer",
    company="Acme",
    url="https://jobs.example.com/acme/swe",
    ats="greenhouse",
    description="Requirements:\n- 1+ years of professional experience\n",
)

_REPLY = (
    '{"band": "entry", "min_years": 1, '
    '"evidence": "1+ years of professional experience", "confidence": "high"}'
)


class _FakeAnthropicClient:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        block = type("_Block", (), {"type": "text", "text": _REPLY})()
        usage = type("_Usage", (), {"input_tokens": 1370, "output_tokens": 120})()
        return type("_Resp", (), {"content": [block], "usage": usage, "stop_reason": "end_turn"})()


def _fake_anthropic_module(built: list[_FakeAnthropicClient]) -> Any:
    def anthropic(*, api_key: str) -> _FakeAnthropicClient:
        made = _FakeAnthropicClient(api_key=api_key)
        built.append(made)
        return made

    return type("_Sdk", (), {"Anthropic": staticmethod(anthropic)})


def test_interpreter_key_from_the_store_reaches_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interpreter is 463 lines that ran in no pipeline; this is the wire."""
    built: list[_FakeAnthropicClient] = []
    monkeypatch.setattr(interp_mod, "import_module", lambda name: _fake_anthropic_module(built))
    secrets = _FakeSecrets({INTERPRETER_PARAM: _KEY})
    interpreter = ClaudeInterpreter(secrets=secrets, secret_id=INTERPRETER_PARAM)

    verdict = interpreter.interpret(_POSTING)

    assert verdict is not None
    assert verdict.evidence_verified
    assert [c.api_key for c in built] == [_KEY]
    assert secrets.key_calls == [(INTERPRETER_PARAM, "COPILOT_INTERPRETER_API_KEY")]


def test_interpreter_with_no_credential_asks_once_for_a_whole_batch() -> None:
    """``interpret_many`` walks hundreds of postings; one "no" must serve them all."""
    secrets = _FakeSecrets()
    interpreter = ClaudeInterpreter(secrets=secrets, secret_id=INTERPRETER_PARAM)

    resolved = interpreter.interpret_many([_POSTING, _POSTING.model_copy(update={"title": "SWE"})])

    assert resolved == {}
    assert len(secrets.key_calls) == 1
    assert interpreter.stats.calls == 0


def test_interpreter_never_resolves_a_credential_for_a_descriptionless_posting() -> None:
    """Workday returns no description; there is nothing to read and nothing to spend."""
    secrets = _FakeSecrets({INTERPRETER_PARAM: _KEY})
    interpreter = ClaudeInterpreter(secrets=secrets, secret_id=INTERPRETER_PARAM)
    blank = _POSTING.model_copy(update={"description": "", "desc_available": False})

    assert interpreter.interpret(blank) is None
    assert secrets.key_calls == []


def test_interpreter_prefers_a_direct_key_and_never_asks_the_store() -> None:
    secrets = _FakeSecrets({INTERPRETER_PARAM: "from-ssm"})
    interpreter = ClaudeInterpreter(
        api_key="from-caller", secrets=secrets, secret_id=INTERPRETER_PARAM
    )

    assert interpreter._resolved_key() == "from-caller"
    assert secrets.key_calls == []


# --- consumer: the Gmail mailbox ---------------------------------------------


def test_mailbox_builds_credentials_from_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_gmail_credentials`` was called from nowhere; this is the missing link.

    The builder is stubbed because google-auth is an optional extra — the wire
    under test is "secret document reaches the builder", not the builder itself.
    """
    seen: list[Mapping[str, str]] = []

    def _stub_builder(secret: Mapping[str, str]) -> str:
        seen.append(secret)
        return "credentials-object"

    monkeypatch.setattr(gmail_mod, "load_gmail_credentials", _stub_builder)
    secrets = _FakeSecrets(documents={GMAIL_SECRET: _GOOD_GMAIL})
    mailbox = GmailMailbox(secrets=secrets, secret_id=GMAIL_SECRET)

    assert mailbox._resolve_credentials() == "credentials-object"
    assert seen == [_GOOD_GMAIL]
    # Cached on the instance: a run reads the inbox and then sends a briefing.
    mailbox._resolve_credentials()
    assert secrets.document_calls == [GMAIL_SECRET]


def test_mailbox_with_no_secret_fails_contained_and_names_the_id() -> None:
    """The inbox half must fail alone, and the error must point at the fix.

    ``DailyBriefingService`` catches this and reports ``inbox_ok: false`` while the
    keyless supply half runs — which is exactly the state the deploy is in today.
    """
    mailbox = GmailMailbox(secrets=_FakeSecrets(), secret_id=GMAIL_SECRET)

    with pytest.raises(RuntimeError, match=GMAIL_SECRET):
        mailbox.fetch_recent(query="newer_than:2d", max_results=5)


def test_mailbox_with_an_injected_service_never_touches_the_secret_store() -> None:
    """Tests and local runs must not need AWS to exercise the mailbox."""
    secrets = _FakeSecrets(documents={GMAIL_SECRET: _GOOD_GMAIL})
    mailbox = GmailMailbox(service=object(), secrets=secrets, secret_id=GMAIL_SECRET)

    assert mailbox._get_service() is not None
    assert secrets.document_calls == []


def test_placeholder_secret_never_becomes_a_credentials_object() -> None:
    """The stack creates this secret empty, and Secrets Manager seeds a random doc.

    Handing that to ``Credentials`` builds an object that fails at the first API
    call with an auth error pointing at Google, not at the seeding step nobody ran.
    """
    assert load_gmail_credentials({"password": "generated-by-cloudformation"}) is None


def test_access_token_without_a_refresh_token_is_rejected() -> None:
    """An access token expires within the hour, so a daily cron would work once."""
    assert missing_gmail_fields({"token": "ya29.short-lived"}) == (
        "refresh_token",
        "client_id",
        "client_secret",
    )
    assert load_gmail_credentials({"token": "ya29.short-lived"}) is None


def test_blank_required_field_is_missing_not_present() -> None:
    """``client_secret: ""`` is how a half-filled secret looks after a paste error."""
    payload = dict(_GOOD_GMAIL, client_secret="   ")

    assert missing_gmail_fields(payload) == ("client_secret",)


def test_missing_google_auth_degrades_to_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel missing from the hand-assembled bundle must degrade, not raise."""
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", None)

    assert load_gmail_credentials(_GOOD_GMAIL) is None


def test_gmail_secret_values_are_never_logged() -> None:
    """The incomplete-secret warning names the missing FIELDS, never their values."""
    with _captured("copilot.adapters.gmail_mailbox") as lines:
        load_gmail_credentials({"refresh_token": "1//never-log-me"})

    joined = "\n".join(lines)
    assert "client_secret" in joined
    assert "never-log-me" not in joined
