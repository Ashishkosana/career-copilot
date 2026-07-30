"""Gmail-backed :class:`~copilot.ports.mailbox.MailboxPort`.

The Google API client is imported lazily; a built ``service`` (or OAuth
``credentials``) may be injected, which is what tests use. The wire-format
helpers — parsing a Gmail message into an :class:`Email` and encoding an
outgoing RFC 2822 message — are pure and unit-tested.

Safety: job replies are only ever created as *drafts*; :meth:`send` exists for
the daily self-briefing email the service sends to the owner.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

from copilot.domain.models import Email


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
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._user_id = user_id

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        if self._credentials is None:
            raise RuntimeError(
                "Gmail credentials are not configured. Inject a built `service` "
                "or `credentials`, or wire load_gmail_credentials() to your "
                "secret store."
            )
        from googleapiclient.discovery import build

        self._service = build(
            "gmail", "v1", credentials=self._credentials, cache_discovery=False
        )
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


def load_gmail_credentials(secret: Mapping[str, str]) -> Any:
    """Build OAuth user credentials from a secret payload.

    TODO(credential-wiring): fetch this ``secret`` from AWS Secrets Manager
    (``Settings.gmail_secret_id``) and persist refreshed tokens. The shape below
    matches ``google.oauth2.credentials.Credentials`` so the rest of the adapter
    is already correct once the secret plumbing lands.
    """
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=secret.get("token"),
        refresh_token=secret.get("refresh_token"),
        token_uri=secret.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=secret.get("client_id"),
        client_secret=secret.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
