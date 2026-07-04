# Career Copilot

A personal job-search agent that ends the fragmentation. Instead of hopping
between LinkedIn, Gmail, job boards, and a tracker in separate sessions, it does
the prep and hands you **one daily briefing** with a 30-minute action plan — so
the search takes minutes a day instead of hours.

Built to solve my own job search, on the stack I work in:
**Python · Gmail API (OAuth) · AWS serverless (Lambda · DynamoDB · API Gateway ·
Cognito · CDK) · Apify · Claude API.**

## What it does
- **Scans your inbox** (Gmail API) and **triages** it — separates real
  application/recruiter mail from noise, and flags what **needs a reply today**.
- **Surfaces today's job matches** — top-scored, de-duped roles. Locally these
  come from the [`ja`](https://github.com/Ashishkosana) job-apply store; in the
  cloud from a scheduled **Apify** scraper, keyword-scored against the profile.
- **Drafts replies with Claude** for interview/recruiter mail — created as
  **Gmail drafts you review**, never auto-sent.
- **Renders one briefing** (Markdown, optional email) with a focused daily plan.

Every LLM/scraper dependency **degrades gracefully**: without a key, that
feature is skipped and the briefing still ships.

## Architecture
Mirrors a production serverless app (the Crewtron pattern):

```
Flutter app ──JWT──▶ API Gateway (Cognito authorizer) ──▶ Lambda: GET /briefing
                                                              │
EventBridge daily cron ─▶ Lambda: fetch → triage → jobs → draft → store
                                                              │
                              DynamoDB (PK = userId)   Secrets Manager (keys)
```

- `triage.py` / `jobs.py` / `apify.py` / `briefing.py` — pure, unit-tested logic.
- `storage.py` — DynamoDB single table, `userId`-partitioned, with a local-JSON
  fallback for offline dev.
- `auth.py` / `response.py` — JWT `sub` → userId, CORS responses.
- `lambda_handler.py` — the daily cron + the read API.
- `infra/` — CDK stack (DynamoDB, Lambdas, cron, API Gateway, Cognito, Secrets).

## Test it locally (no AWS, no keys required)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .

pytest                        # 38 tests — all logic is verifiable offline

./scripts/run-local.sh        # full pipeline: ja jobs + local JSON store
                              # (renders inbox-free unless credentials.json exists)
```

The CLI directly:
```bash
copilot briefing --no-gmail   # offline briefing (no Gmail creds needed)
copilot briefing              # scan last 24h of mail (needs credentials.json)
copilot briefing --email      # also email the briefing to yourself
```

Optional keys light up more features (all optional):
`ANTHROPIC_API_KEY` (Claude drafts) · `APIFY_TOKEN` (cloud scraper) ·
`JA_DB_PATH` (local job matches) — see `.env.example`.

## Deploy (AWS)
```bash
cd infra && npm install
npx cdk deploy                       # DynamoDB · Lambdas · cron · API · Cognito
AWS_PROFILE=<personal> ./scripts/seed-secrets.sh   # push local keys → Secrets Manager
```

## Roadmap
- **Backend** — Gmail triage · job engine · Apify scraper · Claude drafts ·
  Cognito/DynamoDB · daily cron ✅
- **Next** — deploy + wire keys; **Flutter iPhone app** (TestFlight) consuming
  the API; push notifications.

## Why it's built this way
The final "apply" / "connect" / "send" clicks stay human on purpose —
auto-submitting applications, bulk-connecting, and blind-sending violate
platform terms and risk account bans. The copilot removes the prep and
busywork; you keep the judgment.
