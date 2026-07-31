#!/usr/bin/env bash
# Run the full v2 daily pipeline locally — no AWS, no API key.
#
# What this replaced, and why: the previous version invoked `copilot briefing`, the
# v1 console script (`career_copilot.cli`), with JA_DB_PATH pointing at a job-apply
# SQLite database that no longer exists — the very source whose absence made v1 fall
# through to a bundled 4-row fixture of invented companies. There is no `copilot` on
# PATH in a v2 checkout, so following this file produced "command not found", in a
# public repo, as the documented way to try the project.
#
# Everything below is keyless. Greenhouse, Ashby, Lever, Workable and Workday are
# public unauthenticated APIs; the gates are regex; the store is a SQLite file. The
# inbox half is skipped unless Gmail credentials are injected (nothing in src/ fetches
# the secret yet) and the run reports `inbox_ok: false` rather than dying.
#
# Usage: ./scripts/run-local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=backend/.venv/bin/python
[ -x "$PY" ] || {
  echo "No backend venv at $PY. Create it:"
  echo "  python3.13 -m venv backend/.venv"
  echo "  backend/.venv/bin/pip install -e 'backend[dev,adapters]'"
  exit 1
}

# Empty selects SQLite over DynamoDB — see config.py, `postings_table_name`.
export COPILOT_POSTINGS_TABLE_NAME="${COPILOT_POSTINGS_TABLE_NAME:-}"
export COPILOT_POSTINGS_DB_PATH="${COPILOT_POSTINGS_DB_PATH:-data/postings.db}"
export COPILOT_MY_EMAIL="${COPILOT_MY_EMAIL:-}"

echo "watchlist : $("$PY" -c 'import json; print(len(json.load(open("data/watchlist.json"))["companies"]))') boards"
echo "store     : $COPILOT_POSTINGS_DB_PATH"
echo "email     : ${COPILOT_MY_EMAIL:-(none — the digest is not sent, which is not an error)}"
echo

# The cron handler is the entry point, driven directly. Only the v1 briefing table
# needs AWS, so that one port is faked: the point of a local run is the supply half,
# and a DynamoDB write is not it.
PYTHONPATH=backend/src "$PY" - <<'PY'
import json

from copilot.config import load_settings
from copilot.handlers import cron
from copilot.services.daily_briefing import DailyBriefingService


class LocalBriefingStore:
    """StorePort in memory. Keeps a local run from needing an AWS account."""

    def __init__(self) -> None:
        self._briefings: dict[str, object] = {}

    def save_briefing(self, user_id: str, briefing: object) -> None:
        self._briefings[user_id] = briefing

    def latest_briefing(self, user_id: str) -> object | None:
        return self._briefings.get(user_id)

    def save_jobs(self, user_id: str, jobs: object) -> None:
        pass


settings = load_settings()
built = cron.build_service(settings)
service = DailyBriefingService(
    mailbox=built.mailbox,
    store=LocalBriefingStore(),  # type: ignore[arg-type]
    postings=built.postings,
    posting_store=built.posting_store,
    llm=built.llm,
    resume_text=built.resume_text,
    max_jobs=settings.max_jobs,
)
run = cron.run_briefing(service, settings)
print(json.dumps(cron.briefing_response(run), indent=2))
PY
