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

It opens a browser, you approve, and it writes the result **straight into Secrets
Manager**. The token never touches stdout, a file, or your shell history.

That is deliberate. An earlier version printed the payload and asked the reader to
paste it into an ``aws`` command; the first real run ended with a live client_secret
and refresh_token in a chat window. A credential printed to a terminal is a
credential in scrollback and in every screenshot after.
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

#: Where the token goes. Matches the stack's ``GmailSecretName`` output and the
#: ``COPILOT_GMAIL_SECRET_ID`` the cron receives.
SECRET_ID = "career-copilot/gmail"
AWS_PROFILE = "personal"
AWS_REGION = "us-east-1"


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


def _newest_candidate() -> Path | None:
    """The most recently downloaded client-secret file, or ``None``.

    Run with no argument this is what gets used, because the alternative was asking
    the reader to paste a 110-character path. That path wrapped in the terminal every
    single time: once it split `-c` from its value, once it swallowed the space before
    an argument, and once zsh tried to *execute* the JSON file on the second line. A
    long path in an instruction is a defect in the instruction.

    Newest wins, and which one it picked is printed, because the directory
    accumulates a file per attempt: after one secret reset there were three, two of
    them dead.
    """
    found = sorted(
        DOWNLOADS.glob("client_secret_*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    return found[0] if found else None


def main(argv: list[str]) -> int:
    if len(argv) > EXPECTED_ARGC:
        return _usage(argv)

    if len(argv) == EXPECTED_ARGC:
        secrets_file = Path(argv[1]).expanduser()
    else:
        candidate = _newest_candidate()
        if candidate is None:
            return _usage(argv)
        secrets_file = candidate
        print(f"Using the newest client secret in ~/Downloads:\n  {_describe(candidate)}")
        print(f"  {candidate.name}\n")
        print("Check the app name on the consent screen matches this project before approving.\n")

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

    payload = json.dumps(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )

    # Written straight to Secrets Manager. This function used to print the payload
    # and ask the reader to copy-paste it into an `aws` command, and the first person
    # to run it pasted a live client_secret and refresh_token into a chat window
    # within the minute. That was the script's fault: a credential printed to a
    # terminal is a credential in scrollback, in a screenshot, and in whatever the
    # reader pastes it into next. It now never reaches stdout.
    return _store(payload)


def _store(payload: str) -> int:
    """Put the payload in Secrets Manager via stdin, printing nothing sensitive.

    ``--secret-string file:///dev/stdin`` rather than passing the value as an
    argument: an argument is visible in ``ps`` output to every process on the machine
    for the life of the call, and lands in shell history if anyone repeats it.
    """
    import subprocess  # noqa: PLC0415 - only needed on the success path

    cmd = [
        "aws", "secretsmanager", "put-secret-value",
        "--secret-id", SECRET_ID,
        "--secret-string", "file:///dev/stdin",
        "--profile", AWS_PROFILE,
        "--region", AWS_REGION,
    ]
    try:
        done = subprocess.run(cmd, input=payload, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        print(
            "The aws CLI is not on PATH, so the token could not be stored.\n"
            "Nothing was printed — re-run this script once aws is available.",
            file=sys.stderr,
        )
        return 1

    if done.returncode != 0:
        # stderr may name the secret and the profile, neither of which is secret.
        print(f"Could not store the token:\n{done.stderr.strip()}", file=sys.stderr)
        print(
            "\nThe token is still valid but unsaved, and this script deliberately does\n"
            "not print it. Fix the error above and re-run — a second consent is free.",
            file=sys.stderr,
        )
        return 1

    print(f"\nStored in {SECRET_ID} ({AWS_PROFILE}/{AWS_REGION}).")
    print("Nothing was printed to this terminal. Verify presence with:")
    print(f"  aws secretsmanager describe-secret --secret-id {SECRET_ID} "
          f"--profile {AWS_PROFILE} --query LastChangedDate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
