#!/usr/bin/env python3
"""Mint a Gmail refresh token by running the OAuth consent flow once, locally.

Why this exists at all: the cron needs a **refresh** token, and there is no way to
obtain one without a human approving the grant in a browser. Everything else in this
project is scriptable; this step is not, by design of OAuth.

Why a refresh token specifically, and not the access token Google shows first: an
access token expires within the hour. A secret holding only one authenticates
exactly once and then looks like a revoked grant every morning after — which is a
failure that reads as "Google broke" rather than "we stored the wrong field".

Run it from the repo, with the client secret file you downloaded from Google Cloud:

    backend/.venv/bin/python backend/scripts/gmail_token.py ~/Downloads/client_secret_*.json

It opens a browser, you approve, and it prints the three values to store. It writes
nothing to disk and stores nothing in AWS — that is a separate, deliberate step, so
a mistake here costs one re-run rather than a bad secret in the cloud.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

#: Must match ``gmail_mailbox._SCOPES`` exactly. A narrower scope fails at read time
#: and a broader one asks for consent the app never uses. ``gmail.modify`` covers
#: reading threads and creating drafts; it deliberately does **not** include
#: ``gmail.send``, because nothing in this product may send on its own.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

#: argv is the program name plus the client-secret path, and nothing else.
EXPECTED_ARGC = 2

#: Where Google puts the file, so a wrong invocation can name real candidates
#: instead of asking the reader to go find them.
DOWNLOADS = Path.home() / "Downloads"


def _describe(path: Path) -> str:
    """``project_id`` for a client-secret file, or why it cannot be read.

    Only the project id is read. The file also holds ``client_secret``, and this
    runs in a terminal whose scrollback outlives the shell.
    """
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"unreadable ({type(exc).__name__})"
    block = doc.get("installed") or doc.get("web") or {}
    kind = "Desktop app" if "installed" in doc else "Web app — needs Desktop instead"
    return f"{block.get('project_id', '?')}  [{kind}]"


def _usage(argv: list[str]) -> int:
    """A short, actionable error. Never the module docstring.

    Printing the whole rationale on a bad invocation buries the one line that
    matters, and the first real run hit exactly that: the glob
    ``client_secret_*.json`` matched **two** files, so argv was 3, and the reader
    got fifteen lines about OAuth instead of "you passed two files".
    """
    given = argv[1:]
    if len(given) > 1:
        print(f"Pass one file; the glob matched {len(given)}:\n", file=sys.stderr)
    else:
        print("Pass the client-secret JSON you downloaded from Google Cloud.\n",
              file=sys.stderr)

    found = sorted(DOWNLOADS.glob("client_secret_*.json"))
    if found:
        print("Candidates in ~/Downloads:\n", file=sys.stderr)
        for path in found:
            print(f"  {_describe(path)}\n    {path}\n", file=sys.stderr)
        print("Then:\n  backend/.venv/bin/python backend/scripts/gmail_token.py <one path>",
              file=sys.stderr)
    else:
        print(
            "None found in ~/Downloads. In console.cloud.google.com:\n"
            "  APIs & Services -> Credentials -> Create OAuth client ID\n"
            "  Application type: Desktop app  (a Web app client cannot do this flow)\n"
            "  then Download JSON.",
            file=sys.stderr,
        )
    return 2


def main(argv: list[str]) -> int:
    if len(argv) != EXPECTED_ARGC:
        return _usage(argv)

    secrets_file = Path(argv[1]).expanduser()
    if not secrets_file.is_file():
        print(f"No such file: {secrets_file}", file=sys.stderr)
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
    except ImportError:
        print(
            "google-auth-oauthlib is missing. Install the adapters extra:\n"
            '  backend/.venv/bin/pip install -e "backend[adapters]"',
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
    # `access_type=offline` is what actually returns a refresh token, and `prompt`
    # forces the consent screen even if this account has approved before — Google
    # omits the refresh token on a silent re-approval, which produces a token that
    # works today and dies tomorrow.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )

    if not creds.refresh_token:
        print(
            "Google returned no refresh token. That happens when the grant already\n"
            "exists: revoke it at https://myaccount.google.com/permissions and re-run.",
            file=sys.stderr,
        )
        return 1

    # Printed, not stored. The three field names are exactly what
    # `gmail_mailbox.GMAIL_REQUIRED_FIELDS` checks for, so a copy-paste cannot land
    # under the wrong key.
    print("\nStore these three values in the career-copilot/gmail secret:\n")
    print(json.dumps(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        indent=2,
    ))
    print(
        "\nThen, in one command (leading space keeps it out of shell history):\n"
        "  aws secretsmanager put-secret-value --secret-id career-copilot/gmail \\\n"
        "    --secret-string '<paste the JSON above>' --profile personal\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
