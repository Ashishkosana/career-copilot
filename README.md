# Career Copilot

A personal job-search agent that ends the fragmentation. Instead of hopping
between LinkedIn, Gmail, job boards, and a tracker in separate sessions, it does
the prep and hands you **one daily briefing** with a 30-minute action plan — so
the search takes minutes a day instead of hours.

Built to solve my own job search, on the stack I work in:
**Python · Gmail API (OAuth) · AWS serverless (Lambda · DynamoDB · API Gateway ·
Cognito · CDK) · an LLM, optionally.**

**Live worklist:** [jobs.ashishkosana.com](https://jobs.ashishkosana.com) renders
every eligible role from a real run — 880 of 25,294 postings screened, each with its
score components, and the rejection reason for each of the 24,414 that did not make
it. It is a static page with the data baked in and no API to talk to, which the
banner says out loud.

## What it does
- **Fetches real jobs, with no API key.** Greenhouse, Ashby, Lever, Workable and
  Workday all publish unauthenticated job APIs. One sweep of the 819-board
  watchlist reads ~48,000 postings in under a minute. There is no scraper and no
  vendor in this path.
- **Gates on eligibility, categorically.** Not-a-software-role, wrong seniority
  band, clearance required, citizenship/ITAR restricted, employer-will-not-sponsor.
  Eligibility is pass/fail and is **never** turned into a score — a role you cannot
  legally take is not an 80% match. ~2,700 of ~48,000 survive.
- **Scores what survives against the résumé** as a *set*, never a bare percentage:
  which named requirements are covered, which are missing, out of a denominator
  that came from the posting itself, reported as a named tier (exact / strong /
  partial / weak / unscored).
- **Remembers postings between runs**, so the briefing can say what actually
  changed: first seen, last seen, and closed. A role vanishing from a board is a
  real signal and is invisible without history.
- **Scans your inbox** (Gmail API) and **triages** it — separates real
  application/recruiter mail from noise, and flags what **needs a reply today**.
- **Drafts replies with an LLM** for interview/recruiter mail — created as
  **Gmail drafts you review**, never auto-sent.

Every LLM path **degrades to "no result"** without a key, and the keyless path is
the one that does the work. The two halves also fail independently: an unreachable
mailbox costs you the inbox summary, not the day's corpus, and the run reports
`inbox_ok: false` rather than a zero that reads like a quiet morning.

## Architecture

Hexagonal. `domain/` is pure. `ports/` are Protocols. `adapters/` implement them and
import vendor SDKs lazily, inside the method that needs them. `handlers/` are Lambda
entry points with the wiring split out, so tests drive the logic against in-memory
fakes — no cloud, no network.

```
Flutter app ──JWT──▶ API Gateway (Cognito) ──▶ Lambda: GET /worklist, /worklist/{id},
                                                       /excluded, POST /applied,
                                                       GET /briefing
EventBridge daily ──▶ Lambda: fetch 819 boards → screen → sync → close → score → draft
                                                       │
                              DynamoDB: postings (16-way sharded) + briefings
```

- `backend/src/copilot/` — the v2 package: `domain/`, `ports/`, `adapters/`,
  `handlers/`, `services/`. 498 tests, ruff clean, `mypy --strict` clean.
- `backend/src/copilot/adapters/ats/` — the five ATS readers plus the watchlist
  fan-out, which bounds concurrency and contains a dead board rather than raising.
- `infra/` — CDK: two stacks, the postings table with four named-access-pattern
  GSIs, three Lambdas, the daily rule, the routes, and 10 alarms.
- `src/career_copilot/` + `tests/` at the repo root are the **v1** package, kept
  only because the deployed stack predates v2. Nothing in v2 imports them.

## Run it locally (no AWS, no keys)

```bash
python3.13 -m venv backend/.venv
backend/.venv/bin/pip install -e 'backend[dev,adapters]'

cd backend && .venv/bin/python -m pytest        # 498 tests, all offline
cd .. && ./scripts/run-local.sh                # the real pipeline against public boards
```

`run-local.sh` fetches every board on the watchlist, screens, and writes
`data/postings.db`. It needs no key and no AWS account; the inbox half is skipped
and reported as skipped.

Optional keys light up more, and nothing breaks without them — see `.env.example`.
Every variable the code reads is prefixed `COPILOT_`.

## Deploy (AWS)

```bash
cd infra && npm install
./build-lambda.sh                       # required: the stack refuses to synth without the asset
AWS_PROFILE=<personal> npx cdk deploy --all \
  -c myEmail=<your address> \
  -c ownerUserId=<your Cognito sub>
```

Both context values are mandatory and shape-checked at synth: a blank `myEmail`
means "do not email me" and a blank `ownerUserId` stores every briefing under an id
no authenticated read can match — two failures that produce no error at all.

Then `AWS_PROFILE=<personal> ./scripts/seed-secrets.sh`. **Known gap:** nothing in
`backend/src/` fetches a secret yet, so today the only working credential path is a
`COPILOT_*_API_KEY` env var, and the deployed inbox half stays off.

## Why it's built this way

The final "apply" / "connect" / "send" clicks stay human on purpose. Auto-submitting
applications, bulk-connecting and blind-sending violate platform terms, risk account
bans, and cannot be undone. `POST /applied` records that *you* applied and returns
`"submitted": false`; `tests/test_no_auto_submit.py` walks the AST of every shipped
module to prove the codebase has no way to do otherwise.

The other recurring theme is that **shrinkage has to be visible**. An earlier version
of this read a job database that existed on no machine, silently fell through to a
bundled four-row fixture of invented companies, and rendered them as real matches —
with every AWS metric green. So the daily run now reports every count (fetched, new,
known, closed, kept, excluded, sources ok, sources failed), an empty fetch refuses to
mass-close the corpus, and three CloudWatch alarms watch those numbers with
`treatMissingData: BREACHING`, so a run that stops reporting is itself the alarm.
