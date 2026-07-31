"""LLM-backed reply drafter implementing :class:`~copilot.ports.llm.LLMPort`.

Provider-agnostic by design: the hosted-LLM SDK is imported lazily and hidden
behind a small ``client`` seam, so the class name and public contract carry no
vendor. Output is always a *draft* — the service never auto-sends it.

Graceful degradation: with no API key (local dev, missing secret) the internal
generator returns ``None`` and :meth:`draft_reply` yields ``""``, which the
service reads as "no draft" — the daily run still completes.

The key can arrive two ways: handed in directly (tests, a script, ``Settings``
having read the env var) or resolved through a :class:`SecretsPort` from the SSM
parameter the stack names. Direct wins; see :meth:`_resolved_key`.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from copilot.domain.models import Email
from copilot.logging import get_logger
from copilot.ports.secrets import SecretsPort

_LOG = get_logger("copilot.adapters.llm_reply")

#: The direct-key escape hatch, spelled here rather than in the resolver: which
#: env var overrides a credential is the *caller's* configuration contract, and
#: the resolver must stay ignorant of which credential it is fetching. Matches
#: ``Settings.llm_api_key`` under ``env_prefix="COPILOT_"``, so the two paths
#: cannot disagree about what a developer set on their laptop.
API_KEY_ENV = "COPILOT_LLM_API_KEY"

_SYSTEM = (
    "You are helping a software engineer reply to a recruiting email. "
    "Write a short, warm, professional reply in first person. "
    "Confirm interest, offer availability, and keep it under 120 words. "
    "Do not invent specific dates, salary numbers, or commitments."
)


class LlmReplyDrafter:
    """LLMPort that drafts a reply via a hosted LLM (SDK imported lazily)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "gemini-2.0-flash",
        client: Any | None = None,
        secrets: SecretsPort | None = None,
        secret_id: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._secrets = secrets
        self._secret_id = secret_id
        self._key_resolved = bool(api_key)

    def draft_reply(self, email: Email) -> str:
        """Return a draft body, or ``""`` when the LLM is unavailable."""
        return self._generate(email) or ""

    def _generate(self, email: Email) -> str | None:
        client = self._get_client()
        if client is None:
            return None
        resp = client.models.generate_content(
            model=self._model, contents=self._build_prompt(email)
        )
        text = getattr(resp, "text", None)
        if not text:
            return None
        drafted = str(text).strip()
        return drafted or None

    def _resolved_key(self) -> str:
        """The API key to use, or ``""``. Resolved at most once per instance.

        A directly-supplied key short-circuits the lookup entirely — the secret
        store is not consulted at all — which is what keeps the test suite and a
        laptop free of AWS. Absence is cached like presence: this runs inside a
        drafting loop, and a mailbox with twelve recruiter threads must not make
        twelve ``GetParameter`` calls to be told twelve times that there is no key.
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
            # Through ``import_module`` rather than ``from google import genai``
            # so the missing-wheel path is reachable in a test. It became worth
            # testing the moment a key could actually be resolved in the cloud:
            # before that, ``api_key`` was always empty here and this line was
            # dead in every deployment.
            genai = import_module("google.genai")
        except ImportError:
            _LOG.warning("draft_sdk_missing")
            return None
        self._client = genai.Client(api_key=api_key)
        return self._client

    @staticmethod
    def _build_prompt(email: Email) -> str:
        """Assemble the prompt from an email (pure, unit-tested)."""
        return (
            f"{_SYSTEM}\n\n"
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n"
            f"Preview: {email.snippet}\n\n"
            "Draft the reply body only (no subject line):"
        )
