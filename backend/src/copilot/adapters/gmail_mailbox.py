"""Gmail-backed :class:`~copilot.ports.mailbox.MailboxPort`.

The Google API client is imported lazily; a built ``service`` (or OAuth
``credentials``) may be injected, which is what tests use. The wire-format
helpers — parsing a Gmail message into an :class:`Email` and encoding an
outgoing RFC 2822 message — are pure and unit-tested.

Safety: job replies are only ever created as *drafts*; :meth:`send` exists for
the daily self-briefing email the service sends to the owner.

Credentials come from Secrets Manager through a :class:`SecretsPort`.
:func:`load_gmail_credentials` used to be called from nowhere at all — the secret
existed, the stack granted the read, the builder was written, and the wire between
them was missing, which is the entire reason the deployed inbox half has reported
``inbox_ok: false`` every single day. :meth:`GmailMailbox._resolve_credentials` is
that wire.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

from copilot.domain.models import Email
from copilot.logging import get_logger
from copilot.ports.secrets import SecretsPort

_LOG = get_logger("copilot.adapters.gmail_mailbox")

#: Read, draft, and send — ``send`` is needed for the one message this product sends
#: on its own, the owner's daily briefing to himself. It grants nothing narrower, so
#: the "a reply to an employer is only ever a draft" guarantee is enforced in the
#: code and its tests (``tests/test_no_auto_submit.py``), not by this scope. A tuple
#: because this is shared module state, and a shared mutable default is a footgun
#: that costs nothing to remove.
_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)

#: Fields a *daily* cron needs in the secret. A bare ``token`` is not enough: an
#: access token expires within the hour, so a secret holding only one authenticates
#: exactly once and then looks like a revoked grant every morning after. The
#: refresh triple is what survives to tomorrow. ``token_uri`` is omitted on purpose
#: — it has a correct default and is not a credential.
GMAIL_REQUIRED_FIELDS = ("refresh_token", "client_id", "client_secret")


def _message_to_email(message: Mapping[str, Any]) -> Email:
    """Map a Gmail ``users.messages.get`` payload to an :class:`Email` (pure)."""
    payload = message.get("payload", {})
    headers = {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in payload.get("headers", [])
    }
    return Email(
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        snippet=str(message.get("snippet", "")),
    )


def _encode_message(*, to: str, subject: str, body: str) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API (pure)."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailMailbox:
    """MailboxPort backed by the Gmail API (google-api-python-client, lazy)."""

    def __init__(
        self,
        *,
        credentials: Any | None = None,
        service: Any | None = None,
        user_id: str = "me",
        secrets: SecretsPort | None = None,
        secret_id: str = "",
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._user_id = user_id
        self._secrets = secrets
        self._secret_id = secret_id

    def _resolve_credentials(self) -> Any | None:
        """OAuth credentials from the secret store, or ``None``.

        Lazy and at most once per instance: an injected ``service`` or
        ``credentials`` never triggers a lookup, so tests and local runs touch no
        secret store, and a run that never reads the inbox pays nothing.

        ``None`` on every failure — no secret, a placeholder document, a revoked
        grant — because the caller turns that into a contained inbox failure that
        leaves the keyless supply half of the daily run intact. Raising here is how
        this used to take down the whole cron before ``inbox_ok`` existed.
        """
        if self._credentials is not None:
            return self._credentials
        if self._secrets is None:
            return None
        document = self._secrets.secret_json(self._secret_id)
        if document is None:
            return None
        self._credentials = load_gmail_credentials(document)
        return self._credentials

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        credentials = self._resolve_credentials()
        if credentials is None:
            # Named, not generic: "credentials are not configured" with no id sent
            # a reader looking for a bug in this adapter, when the fix is one
            # ``aws secretsmanager put-secret-value`` against the id below.
            raise RuntimeError(
                "Gmail credentials are not configured "
                f"(secret id: {self._secret_id or 'none passed'}). Inject a built "
                "`service`/`credentials`, or pass a SecretsPort and the secret id."
            )
        from googleapiclient.discovery import build

        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    def fetch_recent(self, *, query: str, max_results: int) -> list[Email]:
        svc = self._get_service()
        listed = (
            svc.users()
            .messages()
            .list(userId=self._user_id, q=query, maxResults=max_results)
            .execute()
        )
        emails: list[Email] = []
        for meta in listed.get("messages", []):
            message = (
                svc.users()
                .messages()
                .get(
                    userId=self._user_id,
                    id=meta["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
            emails.append(_message_to_email(message))
        return emails

    def create_draft(self, *, to: str, subject: str, body: str) -> None:
        svc = self._get_service()
        raw = _encode_message(to=to, subject=subject, body=body)
        svc.users().drafts().create(
            userId=self._user_id, body={"message": {"raw": raw}}
        ).execute()

    def send(self, *, to: str, subject: str, body: str) -> None:
        svc = self._get_service()
        raw = _encode_message(to=to, subject=subject, body=body)
        svc.users().messages().send(userId=self._user_id, body={"raw": raw}).execute()


def missing_gmail_fields(secret: Mapping[str, str]) -> tuple[str, ...]:
    """Which of :data:`GMAIL_REQUIRED_FIELDS` the payload does not supply (pure).

    Split out from the builder so the check is testable without google-auth
    installed, and so the answer can be *logged by name* — "the secret is missing
    refresh_token" is a fix, while "Gmail auth failed" is a morning.
    """
    return tuple(f for f in GMAIL_REQUIRED_FIELDS if not (secret.get(f) or "").strip())


def load_gmail_credentials(secret: Mapping[str, str]) -> Any | None:
    """Build OAuth user credentials from a secret payload, or ``None``.

    ``None`` rather than a half-built credential, for two failure modes that both
    really happen:

    * **The placeholder document.** The stack creates ``career-copilot/gmail``
      empty and a human seeds it out of band, so a well-formed JSON object with
      none of the fields we need is a state this code will meet. Handing that to
      ``Credentials`` produces an object that constructs fine and fails at the
      first API call with an auth error that points at Google, not at the seeding
      step that was never run.
    * **A missing wheel.** The Lambda bundle is assembled by hand, so
      ``google-auth`` not being in it is a real deployment outcome and must
      degrade exactly like a missing secret rather than raising ``ImportError``
      out of the middle of a daily run.
    """
    missing = missing_gmail_fields(secret)
    if missing:
        _LOG.warning("gmail_secret_incomplete", extra={"extra_fields": {"missing": list(missing)}})
        return None
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        _LOG.warning("gmail_sdk_missing")
        return None

    return Credentials(
        token=secret.get("token"),
        refresh_token=secret.get("refresh_token"),
        token_uri=secret.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=secret.get("client_id"),
        client_secret=secret.get("client_secret"),
        scopes=_SCOPES,
    )
