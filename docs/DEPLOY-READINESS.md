# Deploy readiness

An independent, adversarial verification of the materialised screening view and the read
API built on it. Every number below was produced by running the thing on this machine
against the real 25,294-posting corpus. Nothing here was copied out of a report.

This document exists because the previous run reported itself green and shipped a read
API that returns 504 on **every single request**, which it still does right now:

```
$ curl -s -o /dev/null -w "HTTP %{http_code}  %{time_total}s\n" --max-time 40 \
    "https://<api>/prod/public/worklist?limit=1"
HTTP 504  29.379660s
$ curl … "…/public/worklist?limit=25"
HTTP 504  29.359284s
$ curl … "…/public/worklist"
HTTP 504  29.226023s
```

**Verdict: the fix in the working tree is real and it works. It is not deployed.** The
timing claims reproduce, the two stores are behaviourally identical on the new methods,
no Scan was introduced, the public projection holds under attack, and the not-ready
states answer in microseconds. Seven further defects were found and fixed here — one of
which was that the page generator could not build the site at all.

No AWS mutation, no deploy, no secret value read, no commit.

---

## Works, with the command output proving it

### Every gate is green

```
$ cd backend && .venv/bin/python -m pytest
856 passed in 1.09s

$ .venv/bin/python -m ruff check src tests scripts ../tools
All checks passed!

$ .venv/bin/python -m mypy
Success: no issues found in 77 source files

$ cd infra && npx tsc --noEmit
tsc: clean

$ npx cdk synth -c myEmail=… -c ownerUserId=… -c alarmEmail=…
Successfully synthesized to infra/cdk.out

$ backend/.venv/bin/python tools/ui/build_ui.py --check-js
screening view: reusing generation 20260731T093136.748618Z (25294 screened, 811 eligible, 0.2 h old)
public read API contract: 4 routes OK under /public
snapshot holds 811 of 811 eligible roles
internships: 48 software internships, from 318 postings the internship gate removed
wrote build/ui.html  (1910 KiB)
wrote docs/index.html  (1180 KiB)
EXIT=0
```

Baseline was 710. **856 now**, +146. Nothing was deleted to get there: 141 cases in
`test_dynamodb_posting_store.py` alone, of which 49 run twice — once per store — from one
parametrised fixture.

### The screen, on the real corpus

```
open_postings():        25294 rows in 2.02s
build_screening_view:   39.00s  (1.542 ms/posting)  rows=45158
  extrapolated to 47,538: 73.3s
save_screening:         45158 rows in 0.40s
screening_summary():    0.173 ms
FUNNEL screened=25294 kept=811 excluded=24483 eligible=811 internships=48
       needsLevelCheck=631 gateCountTotal=44299
  internship gate fires=318   collection=48
  rows/posting = 1.79
```

Every figure the materialisation agent reported reproduces: 1.79 rows per posting, 811
kept, 48 internships against 318 internship-gate fires, 44,299 gate fires over 24,483
excluded postings.

### Peak memory of the screen — nobody had measured this

The cron has **2048 MB**, less than the read Lambdas' 3008, and the screen is the biggest
thing it does.

```
baseline RSS               32.7 MB
after open_postings       361.5 MB   (25294 postings)
after build view          362.2 MB   (45158 rows)
extrapolated to 47,538    680.7 MB  vs cron memory 2048 MB
```

Fits with 3× headroom. On DynamoDB add the intermediate list of deserialised items that
`open_postings` holds while it maps them (description strings are shared by reference, so
this is dict overhead, not another 268 MB) — budget ~1 GB of 2048.

### Read timings — real corpus, 25,294 postings / 45,158 view rows, best of 5

Driven through synthetic API Gateway events, through `route()`, with a real
5,167-character résumé loaded. The ceiling is the API Gateway REST integration limit, 29 s.

| read | status | ms | vs 29 s |
|---|---|---|---|
| `GET /excluded` (all 7 gates) | 200 | **1.57** | 18,487× under |
| `GET /excluded?gate=not_a_software_role&limit=100` (22,074-row view) | 200 | **1.90** | 15,293× under |
| `GET /public/excluded` | 200 | **2.88** | 10,078× under |
| `GET /worklist/{id}` | 200 | **8.18** | 3,544× under |
| `GET /public/worklist/{id}` | 200 | **9.48** | 3,059× under |
| `GET /internships?limit=25` | 200 | **129.07** | 225× under |
| `GET /worklist?limit=25` | 200 | **131.98** | 220× under |
| `GET /public/worklist?limit=25` | 200 | **132.82** | 218× under |
| `GET /worklist?limit=25&cursor=…` (page 2) | 200 | **128.41** | 226× under |
| `GET /worklist?postedAfter=…` (row-only filter) | 200 | 135.71 | 214× under |
| `GET /worklist?level=entry` (row-only filter) | 200 | 136.33 | 213× under |
| `GET /worklist?ats=greenhouse` (hydrates the view) | 200 | 145.75 | 199× under |
| `GET /internships?tier=strong` (hydrates + scores 48) | 200 | 242.82 | 119× under |
| `GET /worklist?limit=100` (MAX_LIMIT) | 200 | **509.11** | 57× under |
| `GET /worklist?tier=strong` (hydrates + scores 811) | 200 | **4135.74** | **7× under** |
| not screened yet (auth / public) | 503 | **0.010 / 0.013** | ~2,000,000× |
| view 72 h stale (auth / public) | 503 | **0.026 / 0.029** | ~1,000,000× |

Full walks, the way `build_ui.py` does them: `/worklist` 9 pages / 811 items / 4.19 s,
`/internships` 1 page / 48 items / 0.25 s. Both reconcile — `len(items) == matched` and
`matched == eligibleTotal`.

The two numbers to read together are **1.90 ms and 131.98 ms**. The 1.90 ms is a page of
100 out of the *largest* view (22,074 rows) and it is the same as a page out of a 48-row
view: the store cost does not move with the corpus, which is the whole property. The
131.98 ms is scoring, at ~5.1 ms per posting against the résumé, times the page size — so
a read scales with `limit` and with nothing else. Before this change the *best* case was
39 s, 1.4× **over** the ceiling.

One read does not clear the brief's 5× bar and it is called out in full below: see
**Broken → finding 1**.

### No read path can reach a full-corpus screen

Every call site accounted for:

```
$ grep -rn "open_postings" src/
adapters/sqlite_posting_store.py:410      def open_postings           (definition)
adapters/dynamodb_posting_store.py:964    def open_postings           (definition)
ports/postingstore.py:380                 def open_postings           (port declaration)
adapters/dynamodb_posting_store.py:33,51,55,163                       (docstrings)
handlers/public_api.py:30                                             (docstring)
handlers/worklist_api.py:23                                           (docstring)
services/daily_briefing.py:569            corpus = self.posting_store.open_postings()
```

**One caller, in the cron.** Zero in `handlers/`.

```
$ grep -rn "screen_all" src/
domain/screening.py:286      def screen_all              (definition, no production caller)
services/daily_briefing.py:287                           (docstring saying why it is not used)

$ grep -rn "screen(" src/copilot/handlers/
handlers/worklist_api.py:1448    decision = screen(posting)   ← one posting, in GET /worklist/{id}
```

`build_index`, `WorklistIndex` and the handler-side `_internship_collection` are gone.
Two assertions in the suites keep it that way: a call-log audit over all five routes in
both suites, and an AST walk asserting the module does not *name* `open_postings`.

### Both stores are behaviourally identical — checked independently, on real data

The shared suite runs **49 cases against both implementations** (98 test IDs), covering
every case the brief named plus the new methods:

```
$ pytest tests/test_dynamodb_posting_store.py -o addopts= -v \
    | grep -oE "\[(dynamodb|sqlite)\]" | sort | uniq -c
  49 [dynamodb]
  49 [sqlite]
```

Named cases, each present for both stores: `test_an_empty_fetch_does_not_mass_close`,
`test_an_empty_description_never_overwrites_a_real_one`,
`test_a_reappearing_posting_is_reopened`, `test_first_seen_is_never_overwritten`,
`test_cache_survives_a_refetch`,
`test_a_screen_that_dies_mid_write_leaves_the_previous_view_current`,
`test_rows_under_an_unpublished_generation_are_unreachable`.

Not trusting that, I drove **900 real postings out of the local corpus** through both
stores and diffed every view, page by page:

```
driving 900 real postings through both stores
view: 1725 rows, kept=19, internships=1
summary identical: True
  ats_vendor_demo_board              rows=   0/0    pages=  1/1   identical=True
  citizenship_or_itar_restricted     rows= 143/143  pages= 21/21  identical=True
  employer_will_not_sponsor          rows=  50/50   pages=  8/8   identical=True
  internship_not_full_time           rows=   9/9    pages=  2/2   identical=True
  internships                        rows=   1/1    pages=  1/1   identical=True
  kept                               rows=  19/19   pages=  3/3   identical=True
  not_a_software_role                rows= 793/793  pages=114/114 identical=True
  security_clearance_required        rows= 114/114  pages= 17/17  identical=True
  wrong_seniority_band               rows= 596/596  pages= 86/86  identical=True
hydrated 19 ids identical: True
  dynamodb: refuses an unknown view -> unknown screening view 'kepts'
  sqlite:   refuses an unknown view -> unknown screening view 'kepts'
MISMATCHED VIEWS: none
```

The DynamoDB double ran with a 37-item page size, so every walk crossed page boundaries.

**A failed screen never leaves an authoritative view, in either store**, and the one
difference between them is invisible through the port:

```
  dynamodb: previous view still current=True, doomed generation readable=1 rows
  sqlite:   previous view still current=True, doomed generation readable=0 rows
```

SQLite materialises the row list before it opens a transaction, so a dying producer
writes nothing. DynamoDB's `batch_writer` flushes its buffer on the way out of the `with`
block, so orphan rows land. Neither is reachable — no summary names their generation, and
the summary is the publish — but the DynamoDB orphans are only removed by TTL, which is
why the TTL gap below was a real defect and not paperwork.

### No Scan was introduced

```
$ grep -c "Scan" src/copilot/adapters/dynamodb_posting_store.py
3
  line 26:   "No query in this module is a Scan."          (docstring)
  line 459:  kwargs["ScanIndexForward"] = False            (a query direction)
  line 1037: "there is no Scan here."                      (docstring)

$ grep -rn "\.scan(\|table.scan" src/
(no matches)
```

On the synthesised templates, every DynamoDB action granted anywhere:

```
career-copilot:            ['BatchWriteItem', 'GetItem', 'PutItem', 'Query', 'UpdateItem']
career-copilot-monitoring: []
```

Per function, which is the part that matters:

```
CronFn      postings: BatchWriteItem, GetItem, PutItem, Query, UpdateItem
            briefing: BatchWriteItem, PutItem
WorklistFn  postings: GetItem, Query, UpdateItem
PublicFn    postings: GetItem, Query            ← no write, no credential read
ApiFn       briefing: Query
```

`dynamodb:Scan`, `dynamodb:GetRecords`, `ExportsOutput` and `Fn::ImportValue` are absent
from both templates. **No fifth GSI was added** — the view is a base-table item
collection, so the four indexes are unchanged, and `description` is projected only into
`open-index`, which is the index the cron reads in order to screen.

### The public route holds under attack

I spiked the upstream card — the thing the projection consumes — with `ownerId`,
`appliedAt`, `applied`, `resumeText`, `cognitoSub` and a nested object, and loaded a
résumé containing a canary email address and a canary phone number. Then I hit every
public route and recursively scanned every string at every depth.

```
=== leak scan over every public route (upstream card spiked) ===
  /public/worklist                    200    89248 bytes  prosePublished=False quoteMaxChars=180
  /public/internships                 200    42380 bytes  prosePublished=False quoteMaxChars=180
  /public/excluded                    200   453707 bytes  prosePublished=False quoteMaxChars=180
  /public/excluded?gate=…demo_board   200    58888 bytes  prosePublished=False quoteMaxChars=180
  /public/worklist/{id}               200     1434 bytes  prosePublished=False quoteMaxChars=180

banned keys found:  none
canary strings found (email, phone, résumé prose, any "CANARY"):  none
`description` key at any depth:  none
```

Caps, measured over 600 real evidence quotes:

```
  quote         max=131  (cap 180)   n=600
  reason        max=152  (cap 180)
  levelWhy      max=52   (cap 180)
  card identity max=197  (cap 400)
```

The only public string over 180 characters is `location`, twice, at 212 — a legitimate
multi-city list, `BOUNDED` at 400 by design. No excerpt field approaches its cap.

Non-`GET` is refused **before the store is touched** (the store was wrapped in a tripwire
that records every method call):

```
  POST    /public/worklist         405 'method_not_allowed'     store calls=[]
  PUT     /public/worklist         405 'method_not_allowed'     store calls=[]
  DELETE  /public/excluded         405 'method_not_allowed'     store calls=[]
  PATCH   /public/internships      405 'method_not_allowed'     store calls=[]
  POST    /public/applied          404 'not_found'              store calls=[]
  POST    /applied                 404 'not_found'              store calls=[]
  HEAD    /public/worklist         405 'method_not_allowed'     store calls=[]
```

A full public sweep touches exactly three store methods:

```
  ['postings_by_id', 'screened_page', 'screening_summary']
```

Demo boards, walked to the end of both collections and checked against
`domain.demo_boards.is_demo_tenant`:

```
  /public/worklist         811 rows  demo-board rows=0
  /public/internships       48 rows  demo-board rows=0
```

### The not-ready state is fast and honest in every shape

```
A. empty store, corpus never screened
  /worklist          503  'corpus_not_screened'   0.0103 ms
  /internships       503  'corpus_not_screened'   0.0103 ms
  /excluded          503  'corpus_not_screened'   0.0048 ms
  /public/worklist   503  'corpus_not_screened'   0.0134 ms  Cache-Control='no-store'
  …

B. published view is 72 h old (VIEW_STALE_AFTER_HOURS=48)
  /worklist          503  'screening_view_stale'  0.0260 ms
  /public/worklist   503  'screening_view_stale'  0.0288 ms  Cache-Control='no-store'

C. summary stamped 6 h in the future (clock skew)
  → 503 'screening_view_stale' everywhere, 0.02 ms

D. rows published, summary never written — the live-cron crash shape
  (rows present without a summary: 1)
  → 503 'corpus_not_screened' everywhere, 0.01 ms

E. routes that must survive an unscreened corpus
  GET /worklist/{id}         200
  POST /applied              200
  GET /public/worklist/{id}  200

F. a view written by an older VIEW_VERSION
  screening_summary() -> None
  GET /worklist -> 503 {'error': 'corpus_not_screened'}
```

Never a 500, never a hang, never a 200 carrying zeroes. Errors are `no-store` on the
public route, so a "not ready" cannot outlive the cron run that fixes it.

### Both filtered-walk bounds actually fire

```
budget 0.2s             -> 503 {'error': 'filter_scan_too_slow'}     in 1117 ms
max_rows 100 (view=811) -> 400 {'error': 'too_many_rows_to_filter'}  in 0.108 ms, store pages read = 0
unfiltered read with max_rows=1 -> 200 (count=25)  — the bound only guards filters
```

The row bound refuses from the summary's own total before reading a single row. The
budget's overshoot is exactly one page, as documented: 1.117 s from a 0.2 s budget is
200 rows of scoring.

### The published page's shape did not change

I rebuilt `docs/index.html` from the real corpus and compared its boot object against the
committed one, structurally:

```
BOOT key shape identical: True
shape differences: none
  itemCount:       811 -> 811
  eligibleTotal:   811 -> 811
  internshipTotal:  48 -> 48
  funnel identical: True
  builtAt:              2026-07-31T07:42:57 -> 2026-07-31T09:32:37
  snapshot.generatedAt: 2026-07-31T07:42:57 -> 2026-07-31T09:31:36
```

Same 811 postings (same set), same funnel, every field the template reads still present.
Two adjacent rows sharing an identical `posted_at` swapped places, and the new order is
the correct one: the sort key is `<stamp>#<id>` descending, so ties break on id, and the
committed page predates keyset ordering.

`generatedAt` now means the *screening pass's* timestamp on the three list routes rather
than "now" — a semantic change to a field whose name did not change. It is the right
change (the old value claimed freshness for a pass that could be twenty hours old) and
the page reads it as "screened at".

### Reconciliation is intact

On the wire, entirely from the summary, no count touching the corpus:

```
screened=25294 kept=811 excluded=24483 gateCountTotal=44299 overcount=True needsLevelCheck=631
internship gate on the wire = 318
internshipTotal on the wire = 48   (collection matched=48)
```

Both numbers ship in the same payload, so the 318-vs-48 gap is legible instead of looking
like an off-by-270 bug.

---

## Broken / stubbed / half-done

### 1. `?tier=` will answer 503 on the deployed store, and the reported number was wrong

`handlers/worklist_api.py`, `FILTER_SCAN_BUDGET_SECONDS`. **Highest-value finding, and the
one the local timings hide.**

The read agent reported `?tier=strong` at 4.17 s locally and projected "~7.8 s at the
deployed size and still passes". That projection counts scoring only. On DynamoDB the
filtered walk also hydrates every candidate row, and `postings_by_id` is **one GetItem per
id** — a deliberate choice, documented in that method. I counted the round trips by
instrumenting the in-memory double:

```
read                                          st   Query  GetItem  round-trip @5ms
GET /worklist?limit=25                       200      16       26          0.21 s
GET /worklist?limit=100                      200      16       53          0.34 s
GET /internships?limit=25                    200      16        4          0.10 s
GET /excluded (7 gates, limit=10)            200     112       61          0.86 s
GET /excluded (7 gates, limit=100)           200     112      523          3.17 s
GET /worklist?level=entry (row filter)       200      16       20          0.18 s
GET /worklist?ats=greenhouse (hydrates)      200      16       70          0.43 s
GET /worklist?tier=strong (hydrates+scores)  200      16       54          0.35 s
```

At the deployed corpus the kept view is ~1,524 rows (47,538 × the measured 3.2% keep rate;
the code's own docstring says ~1,520). That is 8 store pages — 8 × 16 = 128 Query calls —
plus **1,524 GetItems**. At the adapter's own ~5 ms per call that is ~8.3 s of sequential
round trips, on top of ~7.8 s of scoring: **~16 s, which the 12 s budget refuses.**

So on the deployed store `?tier=` is expected to return 503 `filter_scan_too_slow`, while
`?ats=` at ~8 s passes. This is the *designed* degradation — a named, bounded, retryable
503 on one optional filter rather than a 29 s death — so requirement 1 still holds. But
the docstring asserted the opposite, and the page sends `tier` whenever a visitor clicks a
match-tier chip.

**Fixed here:** the docstring now states the arithmetic and the expected 503; the page has
a sentence for the code; and a test pins the one-GetItem-per-posting cost with the
deployed-size arithmetic in its docstring, so the day someone batches it the win is
measured rather than assumed.

**Not fixed, deliberately, and this is the recommended next commit:** batch the hydrate.
`BatchGetItem` takes 100 keys per call, turning 1,524 round trips into 16 and the whole
tier scan into ~8 s. The adapter rejected it because `BatchGetItem` lives on the service
resource rather than on a `Table`, which means holding a second boto3 object and growing
the in-memory double a second read path. That is a real change with a real test cost and
it deserves its own commit, not a footnote in a verification pass.

Also note `GET /excluded?limit=100` — reachable from the public route — is 112 queries
plus ~523 GetItems, ~3.2 s deployed, not the 2.88 ms measured locally. Still 9× under the
ceiling, but it is the second-most expensive read and no report mentioned it.

### 2. TTL was declared in the adapter and never enabled on the table — **fixed**

`infra/lib/career-copilot-stack.ts:268`. The adapter writes `expires_at` on every one of
~85,000 daily view rows and its docstring says "the table must have TTL enabled on that
attribute". It was not:

```
$ aws dynamodb describe-time-to-live --table-name career-copilot-postings --profile personal
{"TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}}
```

The materialisation agent flagged this as "needs someone else" and nobody was someone
else. It is not cosmetic: the DynamoDB store demonstrably writes orphan rows when a
publish dies part-way (proved above), and TTL is the only thing that removes them. Fixed,
plus a CI check on the synthesised template — DynamoDB silently ignores TTL on an
attribute no item carries, so a typo here is not an error, it is unbounded growth that
nothing reports.

### 3. The page generator could not build the site — **fixed**

`tools/ui/build_ui.py`. The specified gate failed outright:

```
$ backend/.venv/bin/python tools/ui/build_ui.py --check-js
build failed: GET /worklist returned 503: corpus_not_screened
REAL_EXIT=1
```

The read path now requires a materialised view, the only thing in the repo that publishes
one is the cron, and a laptop has no cron. So the live site could not be regenerated from
the local corpus at all. The read agent's report claims these checkers "pass unmodified
against the real corpus" — true only against a store it had pre-seeded out of band; the
documented command does not work.

Fixed: `build_ui.py` now publishes the view itself when there is none or the one there is
stale, through the *production* builder (`services.daily_briefing.build_screening_view`
plus the store's own `save_screening`), and reuses a good one. A hand-rolled view here
would prove only that the page agrees with a shape the build script invented.

### 4. The page shows visitors raw error codes — **fixed**

`tools/ui/index.template.html`, `REASON_TEXT`. Four new API error codes shipped
(`corpus_not_screened`, `screening_view_stale`, `filter_scan_too_slow`,
`too_many_rows_to_filter`) and none had a sentence, so the page fell through to
`'The API could not be used (corpus_not_screened).'` — a bare code on a public portfolio
site, indistinguishable from a crash, which is the exact distinction those codes exist to
make. The read agent asked for two of these in its handoff; there were four. All four now
have prose.

### 5. Containment made a hard cron failure silent, with nothing alarming on it — **fixed**

`adapters/dynamodb_store.py:229` and `infra/lib/monitoring-stack.ts`. The v1-store agent
correctly stopped `save_briefing`/`save_jobs` from sinking a run, logs
`briefing_store_write_failed` at ERROR, and appends to a public `write_errors` list.
Nothing reads `write_errors`, and there was no metric filter on that message. So the
containment turned the exact failure that killed the first live run into one that produces
a **green** `CronFailed` alarm and no notification at all. Its report says the log shape
means "the infra owner can alarm on it with no code change" — possible, not done.

Fixed: a `career-copilot-BriefingWriteFailed` metric filter and alarm, verified in the
synthesised monitoring template. (`screen_skipped` needs no new alarm — a failed publish
reports `kept: 0`, which `EligibleWorklistTooSmall` already catches.)

### 6. `domain/screening.screen_all` raises on a naive `posted_at` — **fixed**

`domain/screening.py:305`. `kept.sort(key=lambda d: d.posting.posted_at or _EPOCH)` mixes
an aware sentinel with whatever tzinfo an ATS supplied:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

Reproduced with three real-shaped postings. Not on any production path — `daily_briefing`
documents avoiding this function for exactly this reason — but it is an exported domain
function that only its own tests keep alive, and "we route around the broken one" is not a
state to ship. It hides well: a one-element sort never compares anything, so it needs two
kept postings *and* mixed offsets. Fixed with the same coercion the service uses, plus a
test that also asserts the naive posting still sorts in the right place.

### 7. A filtered walk could silently truncate `matched` — **fixed**

`handlers/worklist_api.py`, `_walk_view`. The page loop ended by falling out of
`for _ in range(_MAX_SCAN_PAGES)` and returning what it had. Unreachable today — the view
is refused above `FILTER_SCAN_MAX_ROWS` and the bound covers that many rows with two pages
to spare — but the only way to get there is a store returning short pages while promising
more, and then `matching` is *incomplete* and gets reported as an exact `matched`. A
silently wrong number on the trust surface is the failure this whole rewrite exists to
remove. Now a logged 503, with a test that drives a store which promises another page
forever.

### 8. Not defects, but say them out loud

- **`docs/index.html` at HEAD was built by the 504-ing code.** Its `generatedAt` equals
  its `builtAt`, which only happens when `generatedAt` is "now" — the pre-materialisation
  behaviour. The live site is currently a snapshot from a build whose live path 504s.
- **`screen_all` has no production caller.** 21 lines of exported domain code kept alive by
  its own tests. Either give it the one caller it deserves or delete it.
- **`store.write_errors` has no reader.** The alarm now covers the operational hole, but
  `DailyRun` still cannot say "the briefing did not store", so `cron_complete` reports
  `ok: true` for a run that lost its briefing.
- **`data/watchlist.json` went 819 → 818 companies.** Verified: the removed entry is
  Squarepoint Capital with `tenant: "embed"`, which is not a Greenhouse tenant. Correct
  removal.
- **The authenticated routes send no `Cache-Control` at all**, so a browser may apply
  heuristic caching to them; only the public route sets a policy. Low stakes — that surface
  is one user behind Cognito — but it is an asymmetry, not a decision anyone wrote down.
- **`VIEW_STALE_AFTER_HOURS = 48` means two consecutive cron failures take the public site
  to 503.** That is the intended trade — an honest 503 over a silently stale page — but it
  makes the published site hard-dependent on a daily Lambda, and `CronDidNotRun` is the
  only warning before the site goes dark.

---

## Which agent report overstated what

**`read:handlers`** — three overstatements, one of them material.

1. "`tools/ui/build_ui.py`'s own checkers pass unmodified against the real corpus." They do
   not. The documented command exits 1 with `corpus_not_screened` against
   `data/postings.db`. It passes only against a store pre-seeded out of band, which the
   report does not mention.
2. "`?tier=` … ~7.8 s at the deployed size and still passes." Counts scoring and omits
   ~8.3 s of DynamoDB hydration round trips. The realistic figure is ~16 s, which its own
   12 s budget refuses. The bound is well designed; the projection through it was not
   checked.
3. "(a) the UI needs sentences for `corpus_not_screened` and `screening_view_stale`." It
   introduced **four** new error codes. The other two also reach the page.

Everything else in that report reproduced, including every timing to within a few percent
and the two structural assertions that keep `open_postings` out of the read path. The
`IndexCache` handoff it asked for is done — the alias is deleted and the tool constructs
`SummaryCache`.

**`core:materialize`** — accurate, and its "needs someone else" list was a live grenade.
Every measured number reproduced exactly: 45,158 rows, 1.79 rows/posting, 811/48/318,
44,299 gate fires, 0.40 s to save, sub-millisecond summary read, 16 queries per page. The
overstatement is one of framing: "Nothing breaks without it" for the TTL is wrong in
combination with its own design. Its DynamoDB store writes orphan rows on a failed publish
— I reproduced that — and TTL is the only reaper. Left disabled it is not "~36 MB per
stale day", it is monotonic growth of unreadable rows for the life of the table.

**`core:v1store`** — accurate and well tested. I confirmed both key schemas read-only
(`career-copilot` is `PK`/`SK` with 8 items, `career-copilot-postings` is `pk`/`sk` with 4
GSIs) and the adapter now uses the right ones with `overwrite_by_pkeys`. The overstatement
is the containment handoff: "the infra owner can alarm on it with no code change" reads as
covered when it means possible. Its own report names silent containment as how the bug
survived, and it then shipped a silent containment with no alarm and no reader for
`write_errors`. Fixed here.

**All three** reported "all gates green" while `build_ui.py --check-js` — a gate in the
brief — exited 1. Two of them could not have known; the read agent should have.

---

## What I fixed myself

| file | change |
|---|---|
| `infra/lib/career-copilot-stack.ts` | `timeToLiveAttribute: "expires_at"` on the postings table, with the reasoning and the "TTL on a missing attribute is silently ignored" trap written down |
| `infra/lib/monitoring-stack.ts` | `BriefingWriteFailed` metric filter + alarm on the contained write failure `CronFailed` can no longer see |
| `.github/workflows/ci.yml` | new **`page`** job: seeds a store, builds both pages through the real handlers, `--check-js`, then asserts the page is not empty. Plus `tools/**` in the triggers and a template check that the view's TTL stays enabled |
| `tools/ui/seed_demo_store.py` | new. ~15 postings chosen so the page's invariants are non-trivial in CI: enough kept to page, one software internship, one posting failing two gates, one with no description, one demo board |
| `tools/ui/build_ui.py` | publishes the screening view when there is none or it is stale, via the production builder; reuses a good one; moved to `SummaryCache`; stale docstring corrected |
| `tools/ui/index.template.html` | prose for all four new API error codes |
| `handlers/worklist_api.py` | `_walk_view` refuses instead of returning a truncated `matched`; the `FILTER_SCAN_BUDGET_SECONDS` docstring corrected with the deployed round-trip arithmetic; `IndexCache` alias deleted |
| `domain/screening.py` | `screen_all` no longer raises on a naive `posted_at` |
| `tests/test_screening.py` | `test_one_naive_posted_at_does_not_take_the_whole_batch_down` |
| `tests/test_worklist_api.py` | `test_a_walk_that_never_finishes_refuses_rather_than_reporting_a_short_count`; the `IndexCache` alias test inverted to assert it is gone |
| `tests/test_dynamodb_posting_store.py` | `test_hydrating_is_one_get_item_per_posting_and_that_is_the_cost_to_beat` — pins the cost behind finding 1 |
| `docs/index.html` | rebuilt, so the published page carries the new error sentences |

+3 tests, 853 → 856. Every gate re-run and green.

The new CI `page` job was run locally, step for step, exactly as written:

```
$ python tools/ui/seed_demo_store.py "$RUNNER_TEMP/ci-postings.db"
seeded /tmp/ci-sim/ci-postings.db: 15 new, 0 already known

$ COPILOT_POSTINGS_DB_PATH=… python tools/ui/build_ui.py --check-js --local-out … --public-out …
screening view: none published — screening the local corpus (~40 s)
screening view: published 18 rows over 15 postings — 9 eligible, 1 internships, 6 excluded
public read API contract: 4 routes OK under /public
snapshot holds 9 of 9 eligible roles
internships: 1 software internships, from 2 postings the internship gate removed

$ (the built page is not empty check)
page holds 9 of 9 eligible roles and 1 internships, screened 15
EXIT=0
```

Note what that job honestly does *not* catch: the original 504 was a scaling failure,
invisible at any corpus a runner can hold. The guard for that one is the call-log and AST
assertions in the test suite. This job guards the class of break that happened next — the
page generator answering 503 on every route while every other gate was green.

---

## Exactly what Ashish must do next

### 0. Check which account you are pointed at. This has bitten before.

```
$ aws sts get-caller-identity            # your DEFAULT profile
{"Account": "425680120934", …}           ← WRONG ACCOUNT

$ aws sts get-caller-identity --profile personal
{"Account": "921888034384", "Arn": "arn:aws:iam::921888034384:user/ashish-cli"}   ← correct
```

Your default profile is a different account. Every command below names `--profile
personal` (or `AWS_PROFILE=personal`) explicitly. Do not drop it.

### 1. Review the diff, then commit it

The tree is dirty and uncommitted on purpose — 26 files. Read `handlers/worklist_api.py`,
`ports/postingstore.py` and the two store adapters before committing: you have to be able
to defend the record shape (one row per *(posting, view)* pair, and why screening is
materialised while scoring is not).

### 2. Build the Lambda asset and deploy

```bash
cd ~/projects/career-copilot/infra
./build-lambda.sh
AWS_PROFILE=personal npx cdk deploy --all \
  -c myEmail=<your email> \
  -c ownerUserId=<your Cognito sub> \
  -c alarmEmail=<your email>
```

All three context values are required — the stack throws at synth on a blank `myEmail` or
`ownerUserId`, because a blank means "do not email me" and "store the briefing under a
user id no authenticated read can match", and both are silent.

This deploy carries: the materialised-view read path, the v1 key-schema fix that killed
the first live run, TTL on the postings table, and the new
`career-copilot-BriefingWriteFailed` alarm. Confirm the SNS email subscription if you have
not — until you click it, alarms notify nobody.

### 3. Then wait for a cron run. **The API returns 503 until one completes.**

This is the part that is easy to misread as a failed deploy. The live corpus holds 47,538
postings and **no screening view** — the read path is not going to invent one. Immediately
after deploying, every list route answers

```
503 {"error": "corpus_not_screened"}
```

in about 10 microseconds. That is correct behaviour, not a broken deploy. The view is
built by the cron, once a day. Either wait for the schedule or trigger a run yourself —
that is a mutation, so it is yours to run, not mine.

The run should take ~600 s of its 900 s: ~426 s of board sweep, ~10 s to read the corpus,
~73 s to screen 47,538, and ~3,400 `BatchWriteItem` requests to publish ~85,000 rows. Peak
memory ~1 GB of 2048. In the `cron_complete` log line expect `screen_skipped: ""` and
`view_rows` near 85,000; `screened` non-zero with `view_rows: 0` means the screen worked
and the publish did not, which is a different fault with a different fix.

### 4. Verify the read is actually fixed

```bash
curl -s -o /dev/null -w "HTTP %{http_code}  %{time_total}s\n" \
  "https://<api>/prod/public/worklist?limit=25"
```

Expect `HTTP 200` in well under a second. A 504 means you are still on the old bundle. A
503 `corpus_not_screened` means step 3 has not happened yet.

### 5. Republish the page

```bash
cd ~/projects/career-copilot
backend/.venv/bin/python tools/ui/build_ui.py --check-js
```

The committed `docs/index.html` was built by the 504-ing code path. Rebuilding also picks
up the four new error sentences, which is what lets the live site say "nothing has been
screened yet" instead of showing a visitor a raw error code.

### 6. Optional, and the best next commit

Batch the hydrate in `DynamoDbPostingStore.postings_by_id` (`BatchGetItem`, 100 keys per
call). It turns 1,524 sequential round trips into 16 and is the difference between
`?tier=` working and `?tier=` answering `filter_scan_too_slow` on the deployed corpus. The
test added in this pass measures the before, so the after is provable.
