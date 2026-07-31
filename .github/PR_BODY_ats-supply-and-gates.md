# Real job supply, and seniority as a gate instead of a score

## What & why

Two things were broken, and one of them was worse than a bug.

**The job source was fake.** No `ja` database exists on any machine this runs on, so
`JaJobSource` always fell through to its bundled fixture. The briefing has been rendering four
invented companies — Northwind Labs, Cinderberg, Larkfield, Halcyon — with `jobs.example.com`
links, presented as real matches. Graceful degradation hid it: there was no signal
distinguishing "no jobs today" from "no real source configured".

**The scorer could not be fixed by retuning.** `score()` is
`min(100, sum(weight for kw in text))`, which is monotonically increasing in description
verbosity — and verbosity tracks seniority. A senior JD enumerates
Kubernetes/Terraform/Kafka/mentoring; a new-grad JD says "BS in CS expected 2026". Measured
over 4,515 live postings it had **10% recall** on junior roles and **2.6% precision**, and
**0 of the 8 roles it surfaced were junior**. Two of the eight were "AI Solutions Engineer" —
a sales-adjacent role, listed twice, with no dedupe.

This PR replaces both, in four reviewable commits:

| Commit | What |
|---|---|
| `fdf3565` | Stop committing personal data to a public repo |
| `2a07690` | Fetch real jobs from ATS boards instead of a 4-row fixture |
| `946fbf4` | Make seniority a gate, not a score |
| `819ca66` | Remember postings between runs so the digest can say what changed |

### 1. Supply: fetch from the ATS layer, not from job boards

Roughly 84% of the inventory on the aggregators I already use resolves to a handful of ATS
hosts — and the aggregators strip the apply URL, the timestamps and the description on the way
in. So aggregators are now a *discovery* input, never a *fetch* input.

Five adapters against open, unauthenticated, free ATS APIs: **Greenhouse, Ashby, Lever,
Workable** (cross-company meta-search *and* per-company widget) and **Workday**. Each splits a
pure `parse()` from the fetch, so response quirks are unit-tested offline.

The quirks are the substance, and each one silently corrupted a gate before it was handled:

- Greenhouse `content` is **escaped** markup, so it needs unescaping before any text gate.
- Workday's list endpoint returns **no description at all**, and deep offsets **wrap instead of
  ending** — a naive `while offset < total` loop never terminates. It stops on a repeated
  `externalPath` set.
- A minority of Lever records return an empty `descriptionPlain`.
- Workable's page cursor is **`pageToken`**, not `page_token`; the snake_case spelling is
  silently ignored and re-serves page one.

`Posting.desc_available` exists because an empty description matches no exclusion pattern, so
unguarded rows silently **pass** every description-based gate.

The watchlist is built by classifying apply-URL hostnames from Simplify's public listings feed:
**819 boards**, 60% of that feed's active listings. Fan-out is concurrency-bounded — these are
other people's job boards — and one dead board cannot sink a run.

**Measured: 25,294 real postings from 404 companies in 39 seconds, zero `example.com` URLs.**

### 2. Screening: categorical gates, and a worklist with no match percentage

Gates run cheapest-first — role family, then seniority band, then eligibility — and a gate can
only **exclude**. A senior role cannot out-rank a junior one because it never enters the same
list. Output is sorted by recency with **no match percentage**, because at a 0.66% base rate the
useful question is "can I apply to this", not "which of these is best".

Eligibility (clearance, citizenship/ITAR, sponsorship) is categorical too, and **quotes the
sentence that triggered it**, so every decision is auditable. Sponsorship is tri-state: most
postings say nothing, and reading silence as refusal would discard most of the market.

Regexes that exist because a simpler version was wrong on real data:

- bare `itar` matched inside **"military"**, "sanitary" and "solitary" — wrongly excluding
  **2,476** roles until word-anchored.
- `intern(?!al)` matched "Identity **Internat**ional".
- "Engineer I" must not match "Engineer II", and a senior marker must beat a level numeral —
  Samsara really does post "Senior Software Engineer I".
- years-of-experience must ignore "4-year degree" and "graduating in 2026".

`LevelVerdict` records **how** a band was decided. 79 of 183 entry roles are classified from a
years-of-experience line rather than the title, so a consumer holding only the label invents a
reason for it and gets it wrong.

`ScreenReport.excluded` is deliberately separate from the per-gate counts, which **overcount by
design**: a role fails several gates at once, so they sum to **43,602** against **24,414** roles
actually removed. The funnel is not a subtraction chain.

**Measured on 25,294 live postings: inversion rate 0, no senior roles surfaced, and 100% recall
over junior roles that are genuinely eligible (149/149).** All 45 exclusions are real clearance,
ITAR or no-sponsorship bars — hand-checked against a 50-row sample.

### 3. Storage: so the digest can say what *changed*

Without persistence the pipeline is amnesiac: it refetches 25k postings, shows the same roles
every morning, cannot say what is new, cannot tell when a role closes, and would re-pay the LLM
for descriptions it already read.

`first_seen` is what "new today" means and is never overwritten. `last_seen` marks a role still
open. `closed_at` is set when a role stops appearing. `interpretation` caches the per-posting LLM
result so a description is read **once**, not once per day — a larger saving than any model choice.

Guards that came out of thinking about failure modes:

- **An empty fetch does not mass-close everything.** That is a broken run, not a market where
  every job vanished on the same morning.
- **An empty description never overwrites a real one**, because Workday returns none for a role
  Greenhouse describes in full.
- A reappearing posting reopens rather than staying closed.

Two consecutive days against real data: day one is 25,294 new and a one-time backlog of 880
applicable; **day two is 3 new, 2 applicable.** The volume problem largely solves itself once
there is history.

### 4. Repo hygiene: this repo is public

- `smoke.dart` hardcoded a Cognito email **and password** as defaults. Credentials now come from
  argv or the environment, and the script exits 2 rather than guessing.
- `main.dart` prefilled a personal email in the sign-in field; now a `--dart-define`.
- The CDK stack hardcoded `MY_EMAIL`; now cdk context or an env var.
- Adds a `private/` convention for the résumé template, tailoring prompts and application answer
  library, with `private.example/` as a committed skeleton so the repo still runs for anyone who
  clones it.
- `.gitignore` now covers `data/postings.db`, which reaches ~150 MB and would be **rejected by
  GitHub's 100 MB limit**.

---

## Backwards compatibility

**No runtime behaviour changes.** `cron.py` still builds the service from `JaJobSource`, so none
of the new supply or screening code is on the Lambda path yet. This PR adds capability; a follow-up
switches the cron over and deletes the fake source.

**Two breaking config renames** — both silent failures if missed:

| Before | After |
|---|---|
| `COPILOT_GEMINI_API_KEY` | `COPILOT_LLM_API_KEY` |
| `gemini_secret_id` | `llm_secret_id` |

An environment still setting the old names gets an empty key, and `LlmReplyDrafter` treats an
empty key as "no LLM available" and returns `""` — so reply drafting would quietly stop rather
than error. Rename before or with deploy.

**`MY_EMAIL` is no longer hardcoded**, so `cdk deploy` *without* the context flag sets it to `""`,
and the service reads empty as "don't email me". **The daily briefing email would stop arriving
with no error.** Deploy as:

```bash
cdk deploy -c myEmail=<address>
```

Public API of the existing domain is untouched: `resolve_level()` keeps its old signature (now a
thin wrapper over `decide_level`), and `ScreenDecision.reasons` still returns a flat tuple
alongside the new `reasons_by_gate`.

---

## Risk & rollback

**Low.** Nothing here is wired into the deployed path, no AWS resources are added, and no
existing endpoint or handler contract changes.

Rollback is `git revert` of any single commit — they are independent and each leaves the test
suite green. Reverting `819ca66` (storage) or `946fbf4` (gates) removes code nothing else calls
yet. Reverting `2a07690` (adapters) would also need the two `data/*.json` files removed.

The residual risks are all outbound-HTTP shaped, and bounded:

- **Politeness to third-party boards.** Concurrency is capped at 6, `Retry-After` is honoured,
  retries are capped at 3 with backoff, and 429 is retried once at most.
- **Board drift.** An ATS changing its response shape degrades to an empty list for that tenant
  and a logged failure, never an exception — `WatchlistPostingSource` contains per-source errors
  and reports them in `FetchReport.failed_sources`.
- **The SQLite file grows to ~150 MB** at 25k postings with full descriptions. Fine locally;
  gitignored. The DynamoDB implementation will need to account for the 400 KB item cap, though the
  largest description measured is ~25 KB, so descriptions do fit.

---

## Testing

`203 passed` · `ruff` clean · `mypy --strict` clean across 42 source files.

- **150 new tests** across four files. Every parser test runs offline against payloads shaped
  like the real ones, and asserts a quirk that actually broke an earlier version — escaped
  markup, missing descriptions, unlisted drafts, epoch milliseconds, the `pageToken` cursor.
- **Regression tests name the real strings**: `military`/`sanitary`/`solitary` must not match
  `itar`; "Identity International" is not an internship; "Senior Software Engineer I" is senior.
- **`TestInversionGate`** is the phase gate: it asserts the exact pair the old scorer got
  backwards (New Grad 36% rejected, Senior Staff 42% accepted) and walks 30 junior/senior pairs
  asserting **zero** inversions.
- **Storage tests cover the failure modes**, not just the happy path: an empty fetch must not
  mass-close, an empty description must not overwrite a real one, a reappearing posting reopens,
  and the LLM cache survives a refetch.
- **Corpus-scale verification** is reproducible via `backend/scripts/eval_screening.py`, which
  reports the funnel, recall, precision and inversion rate against a fetched corpus.

Not covered by automated tests, and deliberately so: the live HTTP calls. Adapter behaviour is
tested against captured payload shapes; the network itself was verified by hand against 22 real
companies across all five ATSs.

---

## Screenshots

**None — this is backend only.** No UI is changed in this PR. The worklist prototype built on
this data lives outside the repo for now and will come with its own PR.

---

## Flags

- **New API endpoint?** No. `handlers/cron.py` changes by one line (the config rename). No route,
  request shape or response shape changes, so no integration-test additions needed.
- **New AWS resource?** **No** — the whole `infra/` diff is the one `MY_EMAIL` line. So **no
  `monitoring-stack.ts` entry is required by this PR.** That flag fires on the follow-up, which
  adds the DynamoDB postings table and the EventBridge fetch schedule.
- **Pre-existing issue found, not fixed here:** the CDK sets `GMAIL_SECRET_ID`,
  `CLAUDE_SECRET_ID` and `APIFY_SECRET_ID`, but `config.py` reads with `env_prefix="COPILOT_"` —
  so it looks for `COPILOT_GMAIL_SECRET_ID`. **These names have never lined up**, meaning the
  deployed Lambda has been falling back to defaults. Out of scope here; must be fixed before the
  v2 handlers are wired to the stack.
- **Security, and not solved by this PR:** the Cognito password removed from `smoke.dart` was
  committed in `c275e3a` and pushed. It is public in git history and **must be rotated**;
  deleting it from the working tree does not recover it.
