# Deploy readiness

An independent verification of the public read route, the internships collection, the
credential tiers and the CDK cutover. Every number below was produced by running the
thing, on this machine, against the real corpus — not read out of a report.

**Verdict: ship it, after the four steps in the last section.** Two real defects were
found and fixed; one of them meant an entire feature was dead code in production. No
deploy, no AWS mutation and no secret value was read in producing this document.

---

## Works, with the command output proving it

### Every gate is green

```
$ cd backend && .venv/bin/python -m pytest
684 passed in 1.07s

$ .venv/bin/python -m ruff check src tests scripts ../tools
All checks passed!

$ .venv/bin/python -m mypy
Success: no issues found in 77 source files

$ cd infra && npx tsc --noEmit
(clean)

$ npx cdk synth -c myEmail=… -c ownerUserId=… -c alarmEmail=…
exit=0

$ backend/.venv/bin/python tools/ui/build_ui.py --check-js
public read API contract: 4 routes OK under /public
snapshot holds 813 of 813 eligible roles
internships: 48 software internships, from 318 postings the internship gate removed
UI_EXIT=0
```

684 tests, up from the 669 that were there when this review started: 15 added here.

### The keyless product works, with no credentials of any kind

Run with `AWS_CONFIG_FILE=/dev/null`, `AWS_SHARED_CREDENTIALS_FILE=/dev/null`, EC2
metadata disabled and every `AWS_*` / `*_API_KEY` variable stripped from the
environment. Every credential lookup returns "no result" and none raises:

```
remaining AWS/key vars: ['AWS_CONFIG_FILE', 'AWS_DEFAULT_REGION',
                         'AWS_EC2_METADATA_DISABLED', 'AWS_SHARED_CREDENTIALS_FILE']
api_key(llm path):    -> ''    (no raise)
api_key(env miss):    -> ''    (no raise)
api_key(no name):     -> ''    (no raise)
secret_json(gmail):   -> None  (no raise)
secret_json(missing): -> None  (no raise)
llm_reply    -> ''
interpreter  -> None
```

Each failure logs a name and a code, never a value:

```
{"level":"WARNING","logger":"copilot.adapters.ssm_secrets","message":"secret_unavailable",
 "store":"parameter","name":"/career-copilot/llm-api-key","code":"NoCredentialsError"}
```

And with no credentials the whole pipeline still runs on the real
`data/postings.db`:

```
screened=25294  kept=813  excluded=24481
scoring available (keyless) = True
exact matches in worklist = 20
```

One thing worth stating precisely, because a report claimed more than is true:
`GmailMailbox.fetch_recent` **does** raise `RuntimeError` when no credential resolves.
That is not a defect — `_resolve_credentials` degrades to `None`, and the raise happens
one layer up in `_get_service`, where the service catches it and reports
`inbox_ok: false` with the supply half of the run intact. The claim "nothing in the
credential path raises" is true of the credential path and false of the mailbox
surface; the containment is real either way and is what matters.

### Internships are separated, not leaked — measured on the real corpus

```
screened      25294
kept (worklist)  813      exact matches in worklist         20
internships       48      exact matches among internships    5
worklist ∩ internships     = 0
INTERN level in worklist   = 0
demo boards in worklist    = 0
demo boards in internships = 0
worklist levels     {'unknown': 632, 'entry': 181}
internship levels   {'intern': 45, 'unknown': 3}
```

The 318 → 48 reconciliation holds, and every dropped posting is dropped by a gate that
has nothing to do with being an internship:

```
internship gate fired on 318 postings
of the 270 dropped: not_a_software_role 265, citizenship_or_itar 20,
                    employer_will_not_sponsor 17, wrong_seniority_band 4,
                    security_clearance 3, ats_vendor_demo_board 12
```

Both numbers ride in every response (`internshipTotal: 48`,
`funnel.gates.internship_not_full_time: 318`) so the 270 difference reads as an
explanation rather than a bug.

### The public route survived an adversarial pass

Every attack below was run in code against the real corpus and the real handler, not
reasoned about. What actually happened:

| Attack | Result |
|---|---|
| Return description prose | **No.** A marker sentence planted in a description appears in none of the four payloads. All 861 `description` values in the published page are `null`. |
| Return an evidence quote over 180 chars | **No.** A 640-char title produced `reason` and `quote` both exactly 180 chars ending `…`. |
| Return any string over the payload bound | **Was yes — fixed.** See "Broken", item 2. Now 0 strings over 400 chars across a 44-payload sweep. |
| Return résumé text | **No.** The real 5,167-char résumé is loaded; none of its 34 prose lines appears anywhere. The score still publishes `Python` — a `gap.VOCAB` token, never document text. |
| Return owner id / applied state / Cognito subject | **No.** Spiking `worklist_api._card` with `ownerId`, `appliedAt`, `applied`, `resumeText` and a canary: all five present upstream (control asserted), all five absent from all four public routes. |
| Return a field the allowlist does not name | **No.** Same spike; the canary `LEAK-CANARY-9999` never appears. |
| Rename an allowlisted field | **500 `public_projection_failed`**, `Cache-Control: no-store`. Fails closed. |
| Turn a scalar into an object | **500**, and `SECRET-NOTE-777` planted inside it does not appear. |
| Write anything | **No.** A full four-route sweep touches exactly `{'open_postings'}`. POST/PUT/PATCH/DELETE/HEAD/TRACE/CONNECT and their lowercase forms → 405, `store.calls == []`, `store.applied == {}`. |
| Reach `POST /applied` by path | **No.** `/public/../applied` → 404. `/public/worklist/../../applied` → POST 405, GET 400 `missing_posting_id`. `/public/applied` → 404. `/PUBLIC/worklist` → 404. |
| Verb-case bypass | **No.** `post`, `PoSt`, `DELETE` all → 405 before dispatch. Absent method defaults to GET, which is safe because only GET reaches a handler. |
| Demo board in worklist or internships | **No**, in either, on the real corpus and on a synthetic demo tenant. |
| Absurd `limit` | 999999999 / -5 / 0 / `abc` / `1e9` → 400 `invalid_limit`. 101 → 400. `MAX_LIMIT` = 100. |
| Crafted cursor | Junk → 400 `invalid_cursor`. Forged fingerprint → 400 `cursor_filter_mismatch`. A worklist cursor on `/internships` and an internships cursor on `/worklist` are both refused — the collection is folded into the fingerprint. |
| Malformed filter | `../../etc/passwd` → 400 `invalid_tier`; `'; DROP TABLE postings;--` → 400 `invalid_level`; `\x00\x01` → 200 with 0 matches; bad date → 400 `invalid_date`; `gate=../../` → 400 `invalid_gate`. |
| Traversal in `{id}` | `../../applied`, `%2e%2e`, `' OR 1=1--`, `\x00`, 5,000 chars → 404 `posting_not_found`. |
| Forged caller identity | Ignored. `authorizer.claims.sub = "ATTACKER-SUB"` plus a bearer header → 200 with no trace of it; `read_event` discards it and substitutes `public-unauthenticated`. The body carries no body, and the query is filtered to the 9-parameter allowlist (`evil` dropped from both containers). |
| Cache poisoning via a cached error | Not possible. 200 → `public, max-age=300`; 400 / 404 / 405 / 500 → `no-store`. |

Two behaviours that look like findings and are not:

- **`GET /public/worklist/{id}` serves excluded postings**, including demo-board ones
  (`kept: false`, with the gate and the quote). That is the trust surface working: the
  exclusion ledger is browsable with evidence by design, and a detail card is how you
  read one. The two *collections* are clean, which is the property that matters.
- **`//public/worklist` and `/public/worklist/` return 200.** Path normalisation, not a
  bypass — both resolve to the same template.

### Layering, parity, publishability

```
$ grep -rn "from copilot.adapters" backend/src/copilot/domain/ backend/src/copilot/ports/
(no matches)
$ grep -rnE "^import (boto3|google|anthropic)" backend/src/copilot/domain/
(no matches)
```

The store parity suite is real and still covers both implementations: 77 tests in
`backend/tests/test_dynamodb_posting_store.py`, parametrised
`@pytest.fixture(params=["dynamodb", "sqlite"])`, and every case the port needs is
there for both — an empty fetch does not mass-close, an empty description does not
overwrite a real one, a reappearing posting reopens, `first_seen` is never overwritten,
the interpretation cache survives a refetch and a close/reopen. The new work touches
neither store (the public route calls `open_postings` and nothing else), so parity was
not extended; it was confirmed.

```
$ git check-ignore -q data/postings.db && echo ok     # ok
$ git check-ignore -q private/ && echo ok             # ok
$ git check-ignore -q .env && echo ok                 # ok
largest tracked file: docs/index.html  1,211,327 bytes
files over 100 MB reachable by git: none
tracked files matching key/token/PEM/password patterns: none
tracked files containing /Users/ashishk: none
```

The published page carries no personal data: 0 occurrences of the personal email, of
`private/`, of `ownerId` or `appliedAt`; `descChars: 0`; `prosePublished: false`; all
861 `description` values `null`.

**Nothing can auto-submit to an employer.** Exactly one outbound HTTP write exists in
the entire backend:

- `backend/src/copilot/adapters/ats/_http.py:46` — `urllib.request.Request(url, data=…)`,
  the only function in the package that can send a body.
- Its only caller is `backend/src/copilot/adapters/ats/workday.py:103`, which posts
  `{"appliedFacets": {}, "limit": …, "offset": …, "searchText": …}` to Workday's job
  *search* endpoint. A read spelled POST because that is the API Workday exposes.

Nothing in `tools/` or `scripts/` makes an outbound write at all.
`backend/tests/test_no_auto_submit.py` enforces this by AST walk over every shipped
module rather than by grep (7 tests, passing), so `getattr(client, "po" + "st")` could
not slip past it.

---

## Broken / stubbed / half-done

### 1. The entire credential tier was dead code in production — FIXED

`backend/src/copilot/handlers/cron.py:63` (before the fix):

```python
mailbox=GmailMailbox(),
llm=LlmReplyDrafter(api_key=settings.llm_api_key),
```

No resolver was passed to either consumer. So:

- `adapters/ssm_secrets.py` (335 lines) and `ports/secrets.py` had **no caller in any
  handler**. 45 tests passed against an adapter nothing constructed.
- The stack granted the cron `ssm:GetParameter` on both key paths plus scoped
  `kms:Decrypt`, and set `COPILOT_LLM_SECRET_ID` and `COPILOT_INTERPRETER_SECRET_ID` to
  those paths — all correct, all unexercised.
- The consequence in the cloud: `inbox_ok` could only ever be `false` and no reply could
  ever be drafted, no matter which parameters Ashish created. Nothing would have failed.
  IAM right, env vars right, adapter right, runtime never calling it.

This is the same shape as the cron bug this project already ate once. A granted
permission that nothing exercises is indistinguishable from a working feature in the
console.

### 2. Every card text field on the public route was unbounded — FIXED

`backend/src/copilot/handlers/public_api.py:153` (before the fix) allowlisted `title`,
`company`, `location` and `url` as plain scalars, while the module docstring claimed:

> Every field that can carry posting text is marked `EXCERPT`, which caps it at
> `QUOTE_MAX_CHARS` **by construction**.

That was false for the four fields that carry the most posting text. Measured:

```
5,000-char title/company/location through the real handler:
  items[0].title    5020 chars   published in full
  items[0].company  5005 chars   published in full
  items[0].location 5007 chars   published in full
  public body        16,245 bytes for ONE row
```

And it already fired on real data — a sweep of 44 public payloads found two strings over
the stated cap, both `location`:

```
[strings over 180 chars] 2 found:
  ('internships', '$.items[16].location', 212)
  ('detail36',    '$.posting.location',   212)
```

Not an exploitable disclosure — an attacker cannot inject a posting, and the snapshot
publishes these fields in full too. It is an unbounded response size on an open metered
endpoint, and a docstring stating a guarantee the code did not provide, on the one file
whose entire value is that its guarantee is exact.

### 3. `infra/build-lambda.sh` would ship an asset without the public handler — FIXED

`infra/build-lambda.sh:181` listed one entry per handler the stack points at, and
`public_api.py` was not among them, though the stack already declared a function whose
handler string is `copilot.handlers.public_api.handler`. The comment directly above that
list describes the exact failure it was failing to catch:

> A stale `build/` directory from the v1 script is indistinguishable from a good one at
> `cdk deploy` time — the deploy succeeds and the Lambda 500s on import.

Worst case for this particular handler: the public route is the one nobody is
authenticated to notice is down, so a stale asset serves 502 to every visitor of
jobs.ashishkosana.com while the authenticated app looks perfectly healthy.

### 4. `Settings.llm_secret_id` / `interpreter_secret_id` defaulted to names that will never exist — FIXED

`backend/src/copilot/config.py:42` and `:47` defaulted to `career-copilot/llm` and
`career-copilot/interpreter` — Secrets Manager-shaped names — while the stack overrides
them with SSM *paths*. Correct in the cloud, where the env var wins; locally they named
a parameter that cannot exist and resolved to `""` forever.

### 5. The level interpreter is wired nowhere — NOT FIXED, and should not be

The port is vendor-neutral (`ports/interpreter.py` names no provider); the adapter module
carries a vendor name in its filename, which is worth renaming for a public repo but is
not what makes it broken.

`backend/src/copilot/adapters/claude_interpreter.py` and
`backend/src/copilot/ports/interpreter.py` exist with tests, and
`DailyBriefingService` (`backend/src/copilot/services/daily_briefing.py:192`) has **no
interpreter field**. `Settings.interpreter_api_key` and
`Settings.interpreter_secret_id` are read by nobody. The stack creates a `CfnOutput`
telling Ashish to create `/career-copilot/interpreter-api-key`, and creating it will
change nothing.

Concretely: 632 of the 813 worklist roles have `level: "unknown"`, and the tier that
exists to resolve them never runs.

Left alone deliberately. Wiring it is a design decision with a cost attached — when do
you interpret, how many per run, what is the cache-miss budget — not a missing line. It
should be its own change.

### 6. The page↔API contract check does not run in CI

`tools/ui/build_ui.py:593` `check_public_contract()` is a genuinely good check: it drives
all four public routes through the real handler and asserts field names, `collection`
values, `prosePublished`, `quoteMaxChars`, the cache and CORS headers, and recursively
that no `description` key exists at any depth. It only runs inside `build_ui.py`, which
needs `data/postings.db` — gitignored. So CI never runs it.

The security-relevant half of that contract *is* covered in CI by
`backend/tests/test_public_api.py`. What is uncovered is narrower and still real: a field
renamed in `public_api`'s projection would show as `undefined` on the live page and no
gate would catch it. Making it CI-runnable means driving it from an in-memory store
instead of the corpus.

### 7. Nothing of v2 is deployed — this is a bigger cutover than "already deployed" suggests

Probed the live API directly:

```
$ curl "https://9iidni6dml.execute-api.us-east-1.amazonaws.com/prod/public/worklist?limit=1"
HTTP 403  {"message":"Missing Authentication Token"}
$ curl "https://9iidni6dml.execute-api.us-east-1.amazonaws.com/prod/worklist?limit=1"
HTTP 403
```

API Gateway returns that for an *unknown route*. The deployed stack is the v1
briefing-only API: `/worklist`, `/worklist/{id}`, `/excluded`, `/internships`,
`/applied` and all four `/public/*` routes are new, and `PostingsTable` does not exist
yet. The published page will keep serving its snapshot and saying so — correctly — until
the deploy lands, and the corpus in the cloud starts empty until the first cron run.

The API id `9iidni6dml` is real in account 921888034384 and the stage is not replaced by
this deploy, so the URL baked into the page at `tools/ui/build_ui.py:132` is correct and
will start answering the moment the stack updates.

---

## Which agent reports overstated what they delivered

**core:api** — two overstatements, one of which was a real defect.

1. The module docstring's claim that "every field that can carry posting text is marked
   `EXCERPT`" was false for `title`, `company`, `location` and `url`. Defect 2 above.
2. "Excerpt caps proven on a 640-char title (`reason` **and** `quote` == 180 chars
   ending `…`)". Both derived fields were capped. The `title` on the same row was
   published at its full 5,032 characters, in the same payload, next to the capped
   quote. The test proved the two fields it looked at.

Everything else in that report checks out, including the numbers: 813 / 20 / 48 / 5,
zero overlap, zero demo boards, `set(store.calls) == {"open_postings"}`, the
cross-collection cursor refusal, and the 405-before-dispatch. The 270-posting
reconciliation is right in substance; "264 are not software roles" measures 265 here,
which is a rounding-level discrepancy in a number that is explanatory, not load-bearing.

**core:secrets** — honest about the blocker, wrong about who would clear it.

The report said the wiring was "Blocked on the handler-wiring agent (I did not touch
`handlers/cron.py`)". There was no handler-wiring agent. So the port, the adapter, the
IAM grants and the env vars all shipped, and nothing connected them: the feature was
100% dead in production and the report reads as though delivery were imminent. The
report also flagged the `config.py` default drift and the two missing `__all__` entries
correctly, and then left all three for a reader who might not exist. Reporting a
dependency is not the same as the dependency being owned by someone.

Also, "**Nothing in the credential path raises**" is true of `api_key` and
`secret_json` and false of `GmailMailbox.fetch_recent`, which raises `RuntimeError`
when no credential resolves. The containment is real one layer up; the sentence is too
broad.

**wire:cdk** — accurate. Every count independently reproduced from the synthesized
templates: 16 `NONE` (12 OPTIONS MOCKs + exactly the 4 public GETs), 6
`COGNITO_USER_POOLS`, 6 `AuthorizerId` refs, `dynamodb:Scan` 0, 5 stage
`MethodSettings` (`/*` at 20/60 and four public at 3/20), `ReservedConcurrentExecutions`
10 on the public function only, 14 alarms, 0 `Fn::ImportValue`, and the public role
holding exactly `[dynamodb:Query, dynamodb:GetItem]` on the table and `/index/*`. The
`/public` CORS override is really `GET,OPTIONS` / `Content-Type` on all five public
resources. The account-resolution hazard is real and reproduced: synth without
`--profile personal` emits SSM ARNs for **425680120934**. The report's own finding 2
noted `build-lambda.sh`'s required-files gap and left it to "that file's owner"; nobody
owned it.

**wire:ui** — accurate, with one claim that has since gone stale.

"`/public/*` is not in `career-copilot-stack.ts` yet" was true when written and is now
false — wire:cdk added it. The *conclusion* still holds for a better reason: the page
falls back today because the routes are not **deployed**, not because they are not in
the stack. Verified counts all reproduce from `docs/index.html`: 813, 20 exact, 48
internships, 318 gate fired, 0 overlap, 0 demo rows, `descChars: 0`,
`prosePublished: false`, `apiPublic: true`, exactly one `method: 'POST'` in the page
behind the `read_only` runtime guard.

---

## What I fixed myself

All in files the component agents were not allowed to touch, except the two defects
inside `public_api.py` and its test file, which had no owner left.

| File | Change |
|---|---|
| `backend/src/copilot/handlers/cron.py` | Build one `AwsSecrets` per invocation and pass it to `GmailMailbox` and `LlmReplyDrafter`. Docstring explains per-invocation rather than module-level (a warm container would keep serving a rotated key) and that keyless stays whole. |
| `backend/src/copilot/config.py` | `llm_secret_id` → `/career-copilot/llm-api-key`, `interpreter_secret_id` → `/career-copilot/interpreter-api-key`, matching what the stack sets. Comments now say which store each id belongs to and why. |
| `backend/src/copilot/handlers/public_api.py` | New `Bounded` spec marker and `CARD_TEXT_MAX_CHARS = 400`; `title`/`company`/`location` truncate at it, `url` fails closed instead (a shortened sentence is still true, a shortened URL is a lie). Docstring rewritten to state the two caps and why there are two, replacing the claim that was not true. |
| `backend/src/copilot/ports/__init__.py` | Export `SecretsPort`. |
| `backend/src/copilot/adapters/__init__.py` | Export `AwsSecrets`. |
| `infra/build-lambda.sh` | Add `copilot/handlers/public_api.py` and `copilot/adapters/ssm_secrets.py` to the required-files list, with a comment on why the public handler is the worst one to omit. |
| `.github/workflows/ci.yml` | New job step asserting five public-route invariants over the **synthesized template**: only GETs under `/public` may be unauthenticated, the 6 Cognito methods are still 6, the public IAM policy contains no write/`ssm:`/`secretsmanager:`/`kms:`/`Scan`, the public methods and the stage are throttled, and the public function has reserved concurrency. Both directions negative-tested (making `/excluded` open, and adding `PutItem` to the public policy, each fail it). |
| `backend/tests/test_handlers.py` | `TestTheCredentialPortIsActuallyWired`, 6 tests. The old test asserted `isinstance(service, DailyBriefingService)`, which stayed true throughout the outage; these assert the edges that carry credentials, that one resolver is shared per invocation and a fresh one per run, that the secret ids match the stack, and that building the service reads nothing. |
| `backend/tests/test_public_api.py` | 4 tests for the new bound: a 5,000-char field truncates at 400, a real 24-city location under the bound is published whole, an oversized `url` raises, and — as a property over a whole-payload string walk — nothing on the wire exceeds the bound. Plus the existing scalar-grows-a-body test parametrised across four fields, because moving `location` to `BOUNDED` changed which branch checks it and would have left the `_scalar` guard untested. |

Rejected on purpose: wiring `ClaudeInterpreter` (a design decision with a cost budget,
not a missing line — see Broken item 5), and moving `check_public_contract` into CI
(worth doing, needs an in-memory driver, not a five-minute change).

Note on the CI step: it duplicates guards the stack already enforces at synth time. That
is intentional. The synth guards live in the same file as the routes they check, so one
commit can relax both; this one cannot be edited by a change to
`career-copilot-stack.ts`.

---

## The exact steps left for Ashish

### 0. The default AWS profile points at the WRONG ACCOUNT

```
$ aws sts get-caller-identity --profile personal
"Account": "921888034384"     ← career-copilot lives here
default profile                → 425680120934     ← someone else entirely
```

Every command below carries `--profile personal`. Without it, `cdk synth` silently emits
SSM ARNs for 425680120934 and a deploy fails on the bootstrap/credential mismatch rather
than mis-deploying — a review hazard, not a security one, but any synth whose ARNs you
intend to *read* must pass the profile too.

### 1. Create the three credentials (all optional; the 813-posting funnel needs none)

Neither SSM parameter exists yet — confirmed by name only, no value read:

```
$ aws ssm describe-parameters --profile personal --region us-east-1
/cdk-bootstrap/hnb659fds/version      ← the only parameter in the account
```

| Id | Store | Type | Unlocks |
|---|---|---|---|
| `/career-copilot/interpreter-api-key` | SSM Parameter Store | **SecureString** | nothing yet — the interpreter is unwired (Broken item 5). Create it when that lands. |
| `/career-copilot/llm-api-key` | SSM Parameter Store | **SecureString** | reply drafting |
| `career-copilot/gmail` | Secrets Manager | JSON with `refresh_token`, `client_id`, `client_secret` | the inbox half / `inbox_ok: true` |

`career-copilot/gmail` already exists and holds the empty CloudFormation placeholder, so
the inbox stays off until you put a real document into it. Note that this deploy changes
its removal policy from `Delete` to `Retain`, so it will survive a future stack delete.

### 2. Deploy

```bash
cd /Users/ashishk/projects/career-copilot/infra
./build-lambda.sh                      # required: synth refuses a stale asset
npx cdk deploy career-copilot career-copilot-monitoring \
  --profile personal \
  -c myEmail=<your email> \
  -c ownerUserId=<your Cognito sub> \
  -c alarmEmail=<your email>
```

`myEmail` and `ownerUserId` are mandatory — the stack throws at synth on a blank one,
because blank means "do not email me" and "store the briefing under a user id no
authenticated read can match", and both are silent failures.

**What this deploy destroys and replaces** — from a real `cdk diff` against the live
stack, not from reading the TypeScript:

```
[-] AWS::SecretsManager::Secret  ClaudeSecret          destroy   ← career-copilot/anthropic
[-] AWS::SecretsManager::Secret  ApifySecret           destroy   ← career-copilot/apify
[-] AWS::ApiGateway::Deployment  Api/Deployment        destroy
[~] AWS::Lambda::Function        CronFn                replace
[~] AWS::Events::Rule            DailyRule             replace
[~] AWS::Lambda::Function        ApiFn                 replace   ← not in the brief's list
[~] AWS::Lambda::Permission      DailyRule/AllowEventRule…  may be replaced
[~] AWS::SecretsManager::Secret  GmailSecret           Delete → Retain
[~] AWS::ApiGateway::Stage       Api/DeploymentStage.prod   modified, NOT replaced
[+] AWS::DynamoDB::Table         PostingsTable
[+] AWS::Lambda::Function        WorklistFn, PublicFn
[+] 4 public GET methods, 5 authenticated methods, 3 log groups, PublicApiUrl output
```

Both `career-copilot/anthropic` and `career-copilot/apify` exist in the account today
and **will be deleted**. If either holds a key you still want, copy it out first — this
is the one irreversible part of the deploy. `ApiFn` being replaced is real and is not in
the brief's list.

Because the stage is modified rather than replaced, the API keeps id `9iidni6dml` and
the URL baked into `tools/ui/build_ui.py:132` starts answering immediately.

### 3. DNS for jobs.ashishkosana.com (Namecheap)

`docs/CNAME` already contains `jobs.ashishkosana.com`. In Namecheap → Domain List →
`ashishkosana.com` → Manage → Advanced DNS → Add New Record:

| Type | Host | Value | TTL |
|---|---|---|---|
| CNAME | `jobs` | `ashishkosana.github.io.` | Automatic |

Then GitHub → repo Settings → Pages → Custom domain → `jobs.ashishkosana.com` → Save,
wait for the DNS check, then tick **Enforce HTTPS**. The page is served by GitHub Pages
and calls the API Gateway URL cross-origin, which the `/public` CORS override allows
(`GET, OPTIONS` / `Content-Type`, origin `*`).

This CNAME points only at GitHub Pages. It does **not** front the API — the page calls
`execute-api.us-east-1.amazonaws.com` directly. If you later put a custom domain on the
API, `PUBLIC_API_BASE` at `tools/ui/build_ui.py:132` is the one line to change; until
then the page falls back to its snapshot and says so in words.

### 4. Confirm the cutover

```bash
# The public route answers, and answers with no prose:
curl -s "https://9iidni6dml.execute-api.us-east-1.amazonaws.com/prod/public/worklist?limit=1" \
  | python3 -m json.tool | head -30

# It is 403 "Missing Authentication Token" today — that is the route not existing yet.

# The authenticated route still demands a token:
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://9iidni6dml.execute-api.us-east-1.amazonaws.com/prod/worklist"   # expect 401

# The corpus starts EMPTY in the cloud. Run the cron once, or wait for DailyRule:
aws lambda invoke --profile personal --function-name <new CronFn name> /tmp/out.json
```

Until that first cron run, `/public/worklist` will correctly answer with 0 items — the
page will fall back to its snapshot and say the API answered but held nothing. That is
the honest state, not a bug, but it means **rebuild the page after the first successful
cron run**, not before:

```bash
backend/.venv/bin/python tools/ui/build_ui.py --check-js
```

Nothing in this review was committed. The tree is dirty; the commits are yours.
