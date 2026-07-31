import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as secrets from "aws-cdk-lib/aws-secretsmanager";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as fs from "fs";
import * as path from "path";

/**
 * Physical names. Fixed on purpose: the monitoring stack builds its alarms from
 * CloudWatch *dimension values*, and passing tokens across stacks would create
 * CloudFormation Exports — which lock the two stacks together for deletion and
 * make "delete the monitoring stack" a reason the app stack cannot be updated.
 * Lambda and DynamoDB are both updated in place, so a fixed name costs nothing
 * here (it only forbids a replacing update, which config/code changes are not).
 */
export const NAMES = {
  briefingTable: "career-copilot",
  postingsTable: "career-copilot-postings",
  cronFn: "career-copilot-cron",
  worklistFn: "career-copilot-worklist",
  publicFn: "career-copilot-public",
  briefingFn: "career-copilot-briefing",
  api: "career-copilot",
  // Pinned for the same reason the function names are: the public-route alarms key
  // on the per-method API Gateway metrics, whose dimensions are (ApiName, Stage,
  // Resource, Method) — and `api.deploymentStage.stageName` is a CfnStage `Ref`
  // token, not a string. "prod" is CDK's own default and is what the live v1 stage
  // is already called, so naming it here pins a fact rather than renaming anything.
  apiStage: "prod",
  gmailSecret: "career-copilot/gmail",
} as const;

/** Deterministic log group names — see {@link NAMES} for why they are fixed. */
const logGroupName = (functionName: string): string => `/aws/lambda/${functionName}`;

/** SSM Parameter Store paths for the two optional LLM credentials. */
export const PARAMS = {
  interpreter: "/career-copilot/interpreter-api-key",
  llm: "/career-copilot/llm-api-key",
} as const;

/** One unauthenticated read route: the API Gateway resource path and its verb. */
export interface PublicRouteRef {
  readonly resourcePath: string;
  readonly httpMethod: string;
}

/**
 * The four unauthenticated read paths, spelled once and consumed three times: to
 * create the methods, to key their per-method throttles, and (via `refs`) as the
 * CloudWatch dimension values the public-route alarms need.
 *
 * Spelled once because a throttle is attached *by path string*, and API Gateway
 * accepts a MethodSetting for a resource that does not exist — no error, no
 * warning, and no throttle. A typo in a second copy of these strings would leave
 * the open endpoint unlimited while the template still looked like it was capped.
 *
 * The values match `KNOWN_PATHS` in `handlers/public_api.py`. That module routes on
 * `event.resource`, which REST API sends as the *template*, so `/public/worklist/{id}`
 * must stay spelled with the braces on both sides.
 */
export const PUBLIC_ROUTES: readonly PublicRouteRef[] = [
  { resourcePath: "/public/worklist", httpMethod: "GET" },
  { resourcePath: "/public/worklist/{id}", httpMethod: "GET" },
  { resourcePath: "/public/internships", httpMethod: "GET" },
  { resourcePath: "/public/excluded", httpMethod: "GET" },
];

/** A request-rate cap: tokens per second, and the bucket a spike can drain. */
export interface ThrottleRef {
  readonly ratePerSecond: number;
  readonly burst: number;
}

/**
 * Per-method throttle on each public route. This is the cost brake
 * `handlers/public_api.py` says it cannot enforce itself, and the reasoning is
 * arithmetic rather than taste:
 *
 * **What a human does.** One page load is three GETs — worklist, internships,
 * excluded — on three *different* methods, so each method sees one request. A
 * detail card adds a fourth on click. Per method, a reader's peak is ~1/s.
 *
 * **Why the burst is 20 and the rate is 3.** A token bucket refuses only when the
 * bucket is empty, so the burst is the number the *humans* touch and the rate is
 * the number only a machine needs. 20 absorbs the shape this page actually gets —
 * a link posted somewhere and twenty first-time readers opening it in the same
 * second — while 3/s sustained is roughly a hundred times any reader's steady rate.
 * A scraper walking all 813 detail cards is held to ~4.5 minutes instead of ~20
 * seconds, which is long enough for the rejection-rate alarm to fire while it is
 * still working.
 *
 * **What it bounds.** 4 routes × 3/s = 12 req/s of public traffic, ~1.04M/day at
 * full saturation: ~$3.60/day of REST requests and, at the ~150 ms a warm request
 * takes, ~470k GB-s ≈ $7.80/day of Lambda. So a *determined, round-the-clock*
 * abuser costs low double-digit dollars a day, and any real day sits inside the
 * free tier. {@link PUBLIC_RESERVED_CONCURRENCY} is the backstop for the case this
 * arithmetic misses — a bug that makes every request slow instead of frequent.
 */
export const PUBLIC_THROTTLE: ThrottleRef = { ratePerSecond: 3, burst: 20 };

/**
 * Stage-wide default, which applies to *every* method including the Cognito ones.
 *
 * It is deliberately higher than the public routes can consume: 12 req/s is the
 * most the four public methods can sustain together, so at least 8 req/s of this
 * bucket is always left for the authenticated app. That interlock is the point —
 * without a stage limit the account default (10,000 req/s) applies and a flood on
 * the open endpoint is unbounded; with a stage limit set *equal* to the public
 * ceiling, a flood would 429 the owner's own reads. Bursts do share the bucket,
 * which is acceptable: a burst is momentary and the sustained rate is what decides
 * whether the app keeps working during an attack.
 */
const STAGE_THROTTLE: ThrottleRef = { ratePerSecond: 20, burst: 60 };

/**
 * Concurrency ceiling on the public function — the second, blunter cost brake.
 *
 * The rate limit bounds *how often* the function is invoked; this bounds how much
 * of the account it can hold at once, which is what protects the two paths that
 * matter if the public route ever gets slow instead of just busy: the daily cron
 * (a briefing that never ran, with no error to show for it) and the owner's own
 * authenticated reads. It also caps the worst case the rate limit cannot: 10
 * containers × 3,008 MB × 86,400 s ≈ 2.6M GB-s ≈ $43/day even if every request
 * somehow ran to the 29s timeout.
 *
 * 10 rather than 3: API Gateway invokes synchronously, so N simultaneous readers
 * need N containers, and a throttled invoke surfaces to the visitor as a 5xx —
 * *not* as a 429. Below ~10 a normal spike would look like an outage. If this
 * deploy ever fails with "decreases account's UnreservedConcurrentExecution below
 * its minimum value", the account's concurrency limit is the thing to check
 * (`aws lambda get-account-settings --profile personal`), not this number.
 */
const PUBLIC_RESERVED_CONCURRENCY = 10;

/** What the monitoring stack needs, as plain strings — never tokens. */
export interface CareerCopilotRefs {
  readonly region: string;
  readonly alarmEmail: string;
  readonly postingsTableName: string;
  readonly cronFunctionName: string;
  readonly worklistFunctionName: string;
  readonly publicFunctionName: string;
  readonly briefingFunctionName: string;
  readonly apiName: string;
  /** Dimension value, not a token — see the note on {@link NAMES}.apiStage. */
  readonly apiStageName: string;
  /** The unauthenticated routes, so their alarms cannot drift from their throttles. */
  readonly publicRoutes: readonly PublicRouteRef[];
  /** The numbers the alarm text quotes, so the description cannot go stale. */
  readonly publicThrottle: ThrottleRef;
  readonly cronLogGroupName: string;
  /** Wall-clock ceiling on the worklist read, so the alarm can sit just under it. */
  readonly worklistTimeoutSeconds: number;
}

/**
 * Read a required deploy-time value, or fail synth.
 *
 * The bug this exists for: MY_EMAIL used to be `tryGetContext("myEmail") ?? ""`.
 * Omit `-c myEmail=…` and the Lambda deploys with an empty string, which
 * `DailyBriefingService.run` reads as "the owner does not want an email" — so
 * the daily briefing silently stops being delivered, with no error anywhere.
 * A blank that changes behaviour must never be deployable.
 */
function requireContext(scope: Construct, key: string, envVar: string, hint: string): string {
  const raw = scope.node.tryGetContext(key) ?? process.env[envVar] ?? "";
  const value = String(raw).trim();
  if (!value) {
    throw new Error(
      `Missing required deploy context "${key}". ${hint}\n` +
        `  Pass it: cdk deploy -c ${key}=<value>   (or export ${envVar}=<value>)`,
    );
  }
  return value;
}

export class CareerCopilotStack extends cdk.Stack {
  public readonly refs: CareerCopilotRefs;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ----------------------------------------------------------------------
    // Deploy-time inputs. Both are personal, so neither is committed; both are
    // required, because both fail *silently* when blank.
    // ----------------------------------------------------------------------
    const myEmail = requireContext(
      this,
      "myEmail",
      "MY_EMAIL",
      "The address the daily briefing and every alarm is sent to. Blank reads as " +
        '"do not email me" and the briefing stops with no error.',
    );
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(myEmail)) {
      throw new Error(`Context "myEmail" is not an email address: ${myEmail}`);
    }
    // OWNER_USER_ID = your Cognito `sub`. The cron writes the briefing under it and
    // the API reads it back keyed on the JWT subject, so a blank here stores every
    // briefing under a user id no authenticated caller can ever match: GET
    // /briefing returns 404 forever and nothing logs a fault. Same class of bug as
    // the blank email, so it gets the same treatment.
    const ownerUserId = requireContext(
      this,
      "ownerUserId",
      "OWNER_USER_ID",
      "Your Cognito user `sub`. Create the user first, then read it with " +
        "`aws cognito-idp admin-get-user`. A blank stores briefings under an id " +
        "no authenticated read can match.",
    );

    // ----------------------------------------------------------------------
    // Lambda asset. Built (Docker-free) by infra/build-lambda.sh.
    // ----------------------------------------------------------------------
    const buildDir = path.join(__dirname, "..", "build");
    // A stale asset is the failure this guard exists for: infra/build is
    // gitignored, so whatever a previous run left there is what deploys. The v1
    // script populated it with `career_copilot/`, and CDK will happily zip that
    // and hand it to a handler string that names `copilot.handlers.*` — a green
    // deploy whose every invocation fails on import.
    for (const required of [
      "copilot/handlers/cron.py",
      "copilot/handlers/worklist_api.py",
      // The public handler is the whole point of the public routes: a stale asset
      // without it synths and deploys clean, and then every anonymous request to
      // jobs.ashishkosana.com is a 502 from an import error on a page with no way
      // to sign in and no error message worth reading.
      "copilot/handlers/public_api.py",
    ]) {
      if (!fs.existsSync(path.join(buildDir, required))) {
        throw new Error(
          `Lambda asset is missing ${required}. Run ./build-lambda.sh before cdk synth/deploy.`,
        );
      }
    }
    const code = lambda.Code.fromAsset(buildDir);

    // ----------------------------------------------------------------------
    // Storage
    // ----------------------------------------------------------------------

    // v1 single table: briefings + the per-user job list the Flutter app reads.
    const briefingTable = new dynamodb.Table(this, "Table", {
      tableName: NAMES.briefingTable,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // v2 posting corpus. A *separate* table from the briefing store, not a
    // preference: `adapters/dynamodb_posting_store` keys items `POSTING#<id>`/
    // `META` in lowercase `pk`/`sk`, while the v1 table's keys are `PK`/`SK` —
    // the schemas are not compatible, and 25k posting items with a projected
    // index have nothing in common with a handful of briefings.
    //
    // Key schema below is transcribed from that adapter, not invented. Every
    // index name and attribute name matches its module constants; changing one
    // here without changing it there produces a ValidationException on the first
    // query, in production, once.
    const postingsTable = new dynamodb.Table(this, "PostingsTable", {
      tableName: NAMES.postingsTable,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      // The corpus is a cache: it rebuilds from public boards in ~39s, so PITR
      // (and its per-GB bill on ~150 MB of description text) buys nothing that a
      // re-fetch does not. `applied_at` is the one irreplaceable attribute here;
      // if that ever grows past a handful of rows, turn PITR on for it.
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // open-index — new_since() (range query on open_sk) and open_postings()
    // (whole partition, full items). INCLUDE rather than ALL: the projection is
    // exactly what `_to_posting` reads, so `interpretation` — an LLM JSON blob no
    // reader here wants — does not double the write cost of save_interpretation.
    // Sparse by construction: closing a posting REMOVEs open_pk/open_sk, so
    // "still open" costs no filter.
    postingsTable.addGlobalSecondaryIndex({
      indexName: "open-index",
      partitionKey: { name: "open_pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "open_sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      // = OPEN_INDEX_PROJECTION in adapters/dynamodb_posting_store.py. `_to_posting`
      // reads the required fields with [], so a missing one raises immediately
      // instead of handing the scorer a posting with an empty description.
      nonKeyAttributes: [
        "id",
        "url",
        "title",
        "company",
        "ats",
        "tenant",
        "location",
        "description",
        "desc_available",
        "req_id",
        "posted_at",
        "remote",
        "employment_type",
        "experience_level",
        "first_seen",
      ],
    });

    // seen-index — sync()'s known/new probe, close_missing()'s open-id
    // enumeration, and the stats totals. KEYS_ONLY because the sort key
    // (`OPEN|CLOSED#<first_seen>#<id>`) already carries everything those three
    // callers need: ~150 bytes per posting instead of 25k GetItems.
    postingsTable.addGlobalSecondaryIndex({
      indexName: "seen-index",
      partitionKey: { name: "seen_pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "seen_sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // cache-index — uncached_ids() and the `interpreted` count. Sparse: the keys
    // are only written by save_interpretation, so the index holds exactly the
    // postings an LLM has already read and never pays for the ones it has not.
    postingsTable.addGlobalSecondaryIndex({
      indexName: "cache-index",
      partitionKey: { name: "cache_pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "cache_sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // applied-index — "what did I apply to, by date". A single partition
    // (`applied_pk = "APPLIED"`, no shard) on purpose: it is written only when a
    // human applies, so it cannot get hot, and one partition keeps the dates in
    // one ordered range.
    postingsTable.addGlobalSecondaryIndex({
      indexName: "applied-index",
      partitionKey: { name: "applied_pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "applied_sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // ----------------------------------------------------------------------
    // Credentials
    // ----------------------------------------------------------------------

    // Gmail stays in Secrets Manager: it is a rotating OAuth refresh token, the
    // one credential here with a lifecycle, and the secret already exists with a
    // token in it (RETAIN, so a stack delete does not take the authorisation with
    // it). Seeded out of band by scripts/seed-secrets.sh — never in code or git.
    const gmailSecret = new secrets.Secret(this, "GmailSecret", {
      secretName: NAMES.gmailSecret,
      description: "Gmail OAuth credentials.json + token.json for the agent",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // The two LLM keys move to SSM Parameter Store (SecureString), which is free
    // at this scale against $0.40/secret/month for a static string that never
    // rotates. They are NOT created here: CloudFormation cannot create a
    // SecureString parameter, and a plaintext String parameter — or a Lambda env
    // var holding the key — would put the key in the synthesised template. The
    // stack passes the parameter *name* and grants a read on exactly that path;
    // resolving it is the runtime's job.
    //
    // Neither parameter has to exist to deploy. Both LLM paths are optional by
    // design: no key means the interpreter returns None and the reply drafter
    // returns "", and the keyless fetch-and-gate pipeline — the one that works
    // today — is untouched.
    //
    // Known gap, narrowed: `adapters/ssm_secrets.py` (`AwsSecrets`) now resolves
    // both of these ids — env var first, then SSM `GetParameter` with decryption,
    // then `""` — and every degradation path is tested. What is still missing is one
    // line of wiring: `handlers/cron.build_service` constructs the LLM adapters
    // without passing the port, so at runtime today the keys are still never read.
    // Until that lands both LLM tiers degrade to "no result", which is the
    // documented behaviour and costs the keyless funnel nothing.
    const parameterArn = (name: string): string =>
      cdk.Stack.of(this).formatArn({
        service: "ssm",
        resource: "parameter",
        // formatArn must not double the slash: SSM parameter ARNs are
        // `…:parameter/career-copilot/x` for a path of `/career-copilot/x`.
        resourceName: name.replace(/^\//, ""),
      });

    // ----------------------------------------------------------------------
    // Cognito — the mobile app signs in here and sends the ID token; API
    // Gateway's authorizer verifies it and injects the `sub` claim that becomes
    // our user id.
    // ----------------------------------------------------------------------
    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: "career-copilot",
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const userPoolClient = userPool.addClient("MobileClient", {
      authFlows: { userSrp: true },
    });

    // ----------------------------------------------------------------------
    // Functions
    //
    // Every env var is COPILOT_-prefixed. `Settings` is a pydantic-settings model
    // with `env_prefix="COPILOT_"`, so the old unprefixed names (TABLE_NAME,
    // GMAIL_SECRET_ID, MY_EMAIL, OWNER_USER_ID, CLAUDE_SECRET_ID,
    // APIFY_SECRET_ID) were read by nothing at all: the deployed Lambda has been
    // running on the defaults in config.py for every one of them since day one.
    //
    // Each function gets only the settings it reads. An env var set on a function
    // that ignores it is not harmless: it is the thing a future reader takes as
    // evidence that a path is wired. The worklist API has no watchlist and never
    // touches the v1 briefing table, so it is not told about either.
    // ----------------------------------------------------------------------
    const commonEnvironment: Record<string, string> = {
      COPILOT_AWS_REGION: this.region,
      PYTHONDONTWRITEBYTECODE: "1",
    };
    // Absolute, because Settings derives its default from config.py's *file
    // location*: under /var/task that resolves to `/data/watchlist.json` and
    // `/private`, and a missing watchlist reads as "no boards to poll" rather
    // than as an error. Both are copied into the asset by build-lambda.sh.
    const WATCHLIST_PATH = "/var/task/data/watchlist.json";
    const PRIVATE_DIR = "/var/task/private";

    const worklistTimeout = cdk.Duration.seconds(29);

    // Dedicated log groups instead of the implicit ones: retention is set without
    // the `logRetention` custom resource (a second Lambda to maintain), and the
    // name is deterministic, which is what lets the monitoring stack attach a
    // metric filter without a cross-stack export.
    //
    // Do NOT set `loggingFormat: LoggingFormat.JSON` on these functions. It looks
    // like an upgrade and it is not: `copilot.logging` already writes one JSON
    // object per event to stdout, and Lambda's JSON format would wrap that object
    // in a second one as a *string* field — at which point `$.fetched` no longer
    // resolves and the supply alarms in the monitoring stack silently stop
    // matching. Which is, precisely, the class of failure they exist to catch.
    const logGroupFor = (id: string, functionName: string): logs.LogGroup =>
      new logs.LogGroup(this, id, {
        logGroupName: logGroupName(functionName),
        // The structured `cron_complete` line is the audit trail for "did supply
        // shrink"; a month of daily runs is enough to see a trend and cheap.
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
    const cronLogGroup = logGroupFor("CronLogGroup", NAMES.cronFn);

    // Daily cron: inbox -> triage -> drafts, and fetch -> screen -> sync -> close.
    const cronFn = new lambda.Function(this, "CronFn", {
      functionName: NAMES.cronFn,
      runtime: lambda.Runtime.PYTHON_3_13,
      code,
      handler: "copilot.handlers.cron.handler",
      // 404 public boards, fanned out 6 at a time, each with a 20s HTTP timeout
      // and 3 attempts. A local sweep is 39s; a handful of boards timing out
      // instead of answering is minutes, and this must not be the thing that
      // caps supply. 15 min is the Lambda ceiling.
      timeout: cdk.Duration.minutes(15),
      // 25,294 postings with full descriptions is ~150 MB of text held while the
      // funnel runs. Memory also buys CPU and network on Lambda, which is what
      // the fan-out is actually bound by.
      memorySize: 2048,
      logGroup: cronLogGroup,
      environment: {
        ...commonEnvironment,
        COPILOT_TABLE_NAME: briefingTable.tableName,
        COPILOT_POSTINGS_TABLE_NAME: postingsTable.tableName,
        COPILOT_WATCHLIST_PATH: WATCHLIST_PATH,
        // The cron scores what reaches the briefing, so it reads the résumé too.
        COPILOT_PRIVATE_DIR: PRIVATE_DIR,
        COPILOT_MY_EMAIL: myEmail,
        COPILOT_OWNER_USER_ID: ownerUserId,
        // The literal, not `gmailSecret.secretName`: that getter reassembles the
        // name out of the ARN with a Fn::Split/Fn::Select chain, which resolves
        // to the same string while making the template unreadable.
        COPILOT_GMAIL_SECRET_ID: NAMES.gmailSecret,
        COPILOT_INTERPRETER_SECRET_ID: PARAMS.interpreter,
        COPILOT_LLM_SECRET_ID: PARAMS.llm,
      },
    });

    // Worklist read API: /worklist, /worklist/{id}, /excluded, /applied.
    const worklistFn = new lambda.Function(this, "WorklistFn", {
      functionName: NAMES.worklistFn,
      runtime: lambda.Runtime.PYTHON_3_13,
      code,
      handler: "copilot.handlers.worklist_api.handler",
      // API Gateway REST caps an integration at 29s, so a longer Lambda timeout
      // would only turn a 504 into a 504 that keeps billing.
      timeout: worklistTimeout,
      // A cold container screens every open posting once (IndexCache then serves
      // the rest of the session). 3008 MB is 2 vCPUs, which roughly halves that
      // first request — the difference between fitting inside the 29s ceiling and
      // not. The alarm on p99 duration in the monitoring stack watches this.
      memorySize: 3008,
      logGroup: logGroupFor("WorklistLogGroup", NAMES.worklistFn),
      environment: {
        ...commonEnvironment,
        // The postings table, and nothing else. `postings_table_name` being set is
        // also what selects the DynamoDB store over SQLite in
        // `worklist_api.build_store` — left unset, this function would try to open
        // `/data/postings.db` on a read-only filesystem.
        COPILOT_POSTINGS_TABLE_NAME: postingsTable.tableName,
        COPILOT_PRIVATE_DIR: PRIVATE_DIR,
      },
    });

    // Public read API: /public/worklist, /public/worklist/{id}, /public/internships,
    // /public/excluded. A *separate function* from the worklist reader, not a second
    // set of routes on it, and the separation is the safety property:
    //
    //   - its IAM has no write action at all, so no bug in it can reach a write,
    //     whatever the code does (the authenticated reader still holds the one
    //     conditional UpdateItem that POST /applied needs);
    //   - its concurrency is reserved, so an open endpoint cannot starve the cron;
    //   - its errors, throttles and 5xx are their own alarms, so "the public page is
    //     down" and "the owner's app is down" are never the same page.
    //
    // Same asset, same store, same screening code — the sanitising projection in
    // `handlers/public_api.py` is the only difference in behaviour.
    const publicFn = new lambda.Function(this, "PublicFn", {
      functionName: NAMES.publicFn,
      runtime: lambda.Runtime.PYTHON_3_13,
      code,
      handler: "copilot.handlers.public_api.handler",
      // Same 29s REST integration ceiling as the worklist reader, and for the same
      // reason: this route runs the identical cold-start screening pass.
      timeout: worklistTimeout,
      // 3008 MB = 2 vCPUs. Not generosity: a cold container screens every open
      // posting before it can answer, and at 1 vCPU that does not fit under 29s.
      memorySize: 3008,
      // The cost brake the rate limit cannot express. See the note on
      // PUBLIC_RESERVED_CONCURRENCY for the arithmetic and the deploy-time failure
      // to expect if this account's concurrency limit is not the default 1,000.
      reservedConcurrentExecutions: PUBLIC_RESERVED_CONCURRENCY,
      logGroup: logGroupFor("PublicLogGroup", NAMES.publicFn),
      environment: {
        ...commonEnvironment,
        // Exactly what the authenticated reader gets, and nothing else. No
        // COPILOT_TABLE_NAME (the v1 briefing store is per-user data), no
        // COPILOT_MY_EMAIL, no COPILOT_OWNER_USER_ID: this function has no notion
        // of a caller and must not learn one — `public_api.PUBLIC_PRINCIPAL` is a
        // synthesised label, not an identity.
        COPILOT_POSTINGS_TABLE_NAME: postingsTable.tableName,
        // The résumé is here because scoring needs it: `score` publishes covered /
        // missing *vocabulary tokens* (`domain/gap.VOCAB`), never document text, and
        // the snapshot page already publishes exactly that. The allowlist projection
        // in `public_api.project()` is what keeps the résumé itself off the wire,
        // and `tests/test_public_api.py` proves it by planting an email address and a
        // prose sentence in a résumé and asserting neither appears in any response.
        COPILOT_PRIVATE_DIR: PRIVATE_DIR,
      },
    });

    // v1 read API: GET /briefing.
    const briefingFn = new lambda.Function(this, "ApiFn", {
      functionName: NAMES.briefingFn,
      runtime: lambda.Runtime.PYTHON_3_13,
      code,
      handler: "copilot.handlers.api.handler",
      timeout: cdk.Duration.seconds(10),
      // One query for one briefing. Nothing to hold in memory.
      memorySize: 512,
      logGroup: logGroupFor("BriefingLogGroup", NAMES.briefingFn),
      environment: {
        ...commonEnvironment,
        COPILOT_TABLE_NAME: briefingTable.tableName,
      },
    });

    // ----------------------------------------------------------------------
    // IAM. Per function, and per verb — not one shared role.
    // ----------------------------------------------------------------------

    // The cron is the only principal holding BatchWriteItem on the corpus. Spelled
    // out rather than granted with grantReadWriteData, which would also add Scan,
    // DeleteItem and the DynamoDB Streams actions — and `dynamodb_posting_store`
    // documents, as a property of the design, that no query in it is a Scan. A
    // policy that permits one is a policy that stops the next accidental
    // full-table read from being obvious in a bill instead of a denial.
    cronFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:Query", // seen-index probe, open-index enumeration, stats
          "dynamodb:GetItem", // cached_interpretation
          "dynamodb:BatchWriteItem", // sync(): 25 items per request on a first run
          "dynamodb:PutItem", // the rewrite-whole path when a probed item vanished
          "dynamodb:UpdateItem", // conditional upsert / close / interpretation cache
        ],
        resources: [postingsTable.tableArn, `${postingsTable.tableArn}/index/*`],
      }),
    );
    // The v1 table: the cron writes briefings and jobs and never reads either back
    // (`latest_briefing` is the read API's business, `seen_job_ids` is unused by the
    // pipeline), so it gets no read action at all — a bug here cannot exfiltrate
    // the store. Two verbs, matching `DynamoDbStore`: put_item for the briefing,
    // batch_writer for the jobs. Not DeleteItem: nothing in this system deletes a
    // briefing, and a daily job with delete rights over its own history is how one
    // bad loop erases the record.
    cronFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:PutItem", "dynamodb:BatchWriteItem"],
        resources: [briefingTable.tableArn],
      }),
    );
    gmailSecret.grantRead(cronFn);

    // The read API's DynamoDB rights are spelled out rather than granted with
    // grantReadWriteData, which would also hand it PutItem, DeleteItem and
    // BatchWriteItem. POST /applied needs exactly one conditional UpdateItem;
    // with this policy the read API physically cannot delete a posting or
    // overwrite the corpus, whatever a future bug in it does.
    worklistFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:Query", // open-index / seen-index / cache-index reads
          "dynamodb:GetItem", // cached_interpretation
          "dynamodb:UpdateItem", // mark_applied — conditional, first write wins
        ],
        resources: [postingsTable.tableArn, `${postingsTable.tableArn}/index/*`],
      }),
    );
    // The public function is read-only in IAM, which is the only place that claim is
    // enforceable. `handlers/public_api.py` never names a write method and its tests
    // assert a full sweep of all four routes touches exactly one store method
    // (`open_postings`) — but a test proves what the code does today, and this policy
    // decides what the code *can* do. Two actions, no UpdateItem, no PutItem, no
    // DeleteItem, no BatchWriteItem: POST /applied is unreachable from here even if a
    // future refactor accidentally routed to it, and "which roles he applied to"
    // stays unwritable as well as unpublished.
    //
    // No Scan, deliberately, like every other role in this stack: `open_postings` is
    // a Query on open-index, so a Scan appearing in a policy here would be evidence
    // that someone stopped using the index — and an anonymous route that can Scan a
    // 25k-item table is a bill with a URL.
    //
    // GetItem is granted alongside Query because `cached_interpretation` is a
    // GetItem, and the scoring path reads it when a stored interpretation exists.
    // It is a read of our own derived data, on the same items the route already
    // publishes, so it adds no surface — and no *index* accepts a GetItem, which is
    // why the index ARNs only matter for the Query half.
    publicFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query", "dynamodb:GetItem"],
        resources: [postingsTable.tableArn, `${postingsTable.tableArn}/index/*`],
      }),
    );
    // And nothing else: no secret, no parameter, no KMS key, no v1 table. An
    // unauthenticated function holding a credential read is how a public route
    // becomes an exfiltration route, so the LLM keys and the Gmail refresh token are
    // granted to the cron only — see the two statements below.

    // GET /briefing is one Query on one partition — `latest_briefing`, limit 1,
    // descending. grantReadData would add Scan and the DynamoDB Streams reads on
    // top of it; there is no Scan permission anywhere in this stack, which is what
    // makes an accidental full-table read fail loudly instead of quietly billing.
    briefingFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query"],
        resources: [briefingTable.tableArn],
      }),
    );

    // Only the cron can read the LLM keys — no LLM runs in a read path. Scoped to
    // the two exact parameter ARNs, so a new parameter under /career-copilot/ is
    // not readable by default. kms:Decrypt is scoped by the ViaService condition
    // to the account's default SSM key, which is what a SecureString created
    // without a customer-managed key is encrypted with.
    cronFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [parameterArn(PARAMS.interpreter), parameterArn(PARAMS.llm)],
      }),
    );
    cronFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["kms:Decrypt"],
        resources: ["*"],
        conditions: { StringEquals: { "kms:ViaService": `ssm.${this.region}.amazonaws.com` } },
      }),
    );

    // ----------------------------------------------------------------------
    // Schedule
    // ----------------------------------------------------------------------
    new events.Rule(this, "DailyRule", {
      ruleName: "career-copilot-daily",
      description: "Daily briefing + ATS sweep",
      // 07:00 America/Phoenix = 14:00 UTC (Arizona has no DST, so this is stable
      // year-round — a cron expressed in local time would drift twice a year).
      schedule: events.Schedule.cron({ minute: "0", hour: "14" }),
      targets: [
        new targets.LambdaFunction(cronFn, {
          // EventBridge retries an async invoke twice by default. This run is
          // idempotent in the store (sync upserts, close_missing is conditional)
          // but NOT in the mailbox: a retry re-sends the briefing email and
          // re-creates reply drafts. One attempt, and the Errors alarm is how a
          // failure is noticed — a silent third copy in the inbox is not.
          retryAttempts: 0,
        }),
      ],
    });

    // ----------------------------------------------------------------------
    // API Gateway. Two authorization regimes on one API:
    //
    //   /briefing, /worklist*, /internships, /excluded, /applied  -> Cognito
    //   /public/*                                                 -> none
    //
    // One API rather than two because the public routes are the *same data* through
    // a sanitising projection, and splitting them would give the page a second
    // hostname, a second stage, a second set of alarms and two throttle budgets to
    // keep in step. The safety property is not "a separate API"; it is that the
    // authorizer is attached per method, the public function's IAM cannot write, and
    // the synthesised template is asserted on both counts.
    // ----------------------------------------------------------------------
    const api = new apigw.RestApi(this, "Api", {
      restApiName: NAMES.api,
      // The website reads this API from a browser, so preflight is served for
      // every resource. Origins are open because the responses are per-user and
      // gated on a bearer token, not on a cookie — there is no ambient
      // credential for a hostile origin to ride.
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: NAMES.apiStage,
        // 5XX/latency alarms need the per-method dimension to be emitted at all.
        metricsEnabled: true,
        // Stage-wide request-rate cap. Without it the account default (10,000 req/s)
        // applies, which for an endpoint anyone on the internet can call is not a
        // limit at all. Sized to leave the authenticated app headroom above
        // everything the public routes can consume — see STAGE_THROTTLE.
        throttlingRateLimit: STAGE_THROTTLE.ratePerSecond,
        throttlingBurstLimit: STAGE_THROTTLE.burst,
        // Access logs and full request tracing are deliberately off: this is a
        // single-user API and CloudWatch ingestion is the line item that grows.
        //
        // Per-method overrides for the public routes. `methodOptions` keys are
        // "/<resource>/<VERB>", built from PUBLIC_ROUTES so a route and its throttle
        // cannot drift apart. metricsEnabled is repeated here rather than inherited
        // from the "/*/*" entry above, because the public-route alarms read the
        // per-method (Resource, Method) metrics and an inherited setting is one
        // refactor away from turning those alarms into permanent INSUFFICIENT_DATA.
        methodOptions: Object.fromEntries(
          PUBLIC_ROUTES.map((route) => [
            `${route.resourcePath}/${route.httpMethod}`,
            {
              metricsEnabled: true,
              throttlingRateLimit: PUBLIC_THROTTLE.ratePerSecond,
              throttlingBurstLimit: PUBLIC_THROTTLE.burst,
            },
          ]),
        ),
      },
    });
    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, "Authorizer", {
      cognitoUserPools: [userPool],
    });
    const authorized = {
      authorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    } as const;

    // GET /briefing -> latest stored briefing (Flutter app).
    api.root.addResource("briefing").addMethod("GET", new apigw.LambdaIntegration(briefingFn), {
      ...authorized,
    });

    const worklistIntegration = new apigw.LambdaIntegration(worklistFn);
    // GET /worklist and GET /worklist/{id}. The handler routes on `event.resource`,
    // which REST API sends as the *template* ("/worklist/{id}"), and reads the id
    // from pathParameters — so these paths must stay spelled exactly like this.
    const worklist = api.root.addResource("worklist");
    worklist.addMethod("GET", worklistIntegration, { ...authorized });
    worklist.addResource("{id}").addMethod("GET", worklistIntegration, { ...authorized });
    // GET /internships — its own path, not `/worklist?collection=internships`: it is
    // a different population with a different denominator (48 software internships,
    // not 813 full-time roles), so its `matched` count is out of a different total.
    // A path is also what makes it linkable, cacheable and throttleable on its own,
    // exactly like `/excluded` is a path rather than `?kept=false`.
    api.root.addResource("internships").addMethod("GET", worklistIntegration, { ...authorized });
    // GET /excluded — the trust surface: what was filtered out, and the sentence
    // that caused it.
    api.root.addResource("excluded").addMethod("GET", worklistIntegration, { ...authorized });
    // POST /applied — records that a human applied. Records only: nothing in this
    // system submits an application, and the function's IAM cannot do more than
    // one conditional UpdateItem.
    api.root.addResource("applied").addMethod("POST", worklistIntegration, { ...authorized });

    // ----------------------------------------------------------------------
    // The public subtree. No authorizer, four GETs, and nothing else.
    //
    // What makes this safe is not that it is a small exposure — it is that it is a
    // *narrower* one than what `docs/index.html` already publishes today, and the
    // narrowing is enforced in three independent places: the allowlist projection in
    // the handler (no prose, no personal fields, excerpts capped at 180 chars), the
    // read-only IAM above, and the method list here. `authorizationType` is written
    // out explicitly rather than left to default, because "no authorizer" must read
    // as a decision in the diff, not as an omission.
    // ----------------------------------------------------------------------
    const publicIntegration = new apigw.LambdaIntegration(publicFn);
    const unauthenticated = { authorizationType: apigw.AuthorizationType.NONE } as const;
    const publicRoot = api.root.addResource("public", {
      // Overrides the API-wide preflight for this subtree only. The API-wide one
      // advertises ALL_METHODS and an Authorization header; on a read-only
      // unauthenticated route both are wrong. A gateway that tells a browser POST is
      // allowed here is a gateway inviting a request that can only ever be a 405 —
      // and advertising `Authorization` invites a page to send a token to an endpoint
      // that has no notion of a caller. GET is a simple request, so a browser will
      // not usually preflight at all; this is what it gets if it does. Matches
      // CORS_HEADERS in `handlers/public_api.py`, whose own _preflight() the gateway
      // MOCK intercepts before the Lambda is ever invoked.
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: ["GET", "OPTIONS"],
        allowHeaders: ["Content-Type"],
      },
    });
    const publicWorklist = publicRoot.addResource("worklist");
    publicWorklist.addMethod("GET", publicIntegration, { ...unauthenticated });
    publicWorklist.addResource("{id}").addMethod("GET", publicIntegration, {
      ...unauthenticated,
    });
    publicRoot.addResource("internships").addMethod("GET", publicIntegration, {
      ...unauthenticated,
    });
    publicRoot.addResource("excluded").addMethod("GET", publicIntegration, {
      ...unauthenticated,
    });
    // ----------------------------------------------------------------------
    // Synth-time guards over the whole method table.
    //
    // These are assertions, not configuration, and they live here rather than in a
    // test because they have to fail the *build* — an authorizer silently dropped
    // from an existing method is the worst outcome in this file, and it is invisible
    // in a diff you skim. `requireContext` above exists for the same reason: a
    // change that reads as harmless must not be deployable.
    // ----------------------------------------------------------------------
    const isPublicPath = (path: string): boolean =>
      path === "/public" || path.startsWith("/public/");

    for (const method of api.methods) {
      // The CORS preflight is a MOCK integration: no authorizer, no Lambda, no store.
      // It is unauthenticated on every resource here and always has been, which is
      // why the count of NONE methods is larger than the count of public routes.
      if (method.httpMethod === "OPTIONS") {
        continue;
      }
      const path = method.resource.path;
      const expected = isPublicPath(path)
        ? apigw.AuthorizationType.NONE
        : apigw.AuthorizationType.COGNITO;
      // Read off the L1 resource because `Method` publishes no authorizationType
      // getter. The child id has been "Resource" for the lifetime of this module, and
      // if that ever changes this guard throws on the cast rather than passing
      // vacuously — which is the failure direction to want.
      const declared = (method.node.defaultChild as apigw.CfnMethod).authorizationType;
      if (declared !== expected) {
        throw new Error(
          `${method.httpMethod} ${path} has authorizationType "${declared}", expected ` +
            `"${expected}". Every route outside /public must stay behind the Cognito ` +
            "authorizer: those responses are per-user and are not filtered by the " +
            "public projection. Only the four /public reads may be anonymous.",
        );
      }
    }

    // PUBLIC_ROUTES is what the throttles and the alarms are built from, so the set
    // of public methods and that list have to be the same set in both directions: an
    // undeclared route is unthrottled and unalarmed, and a declared route that does
    // not exist is a MethodSetting API Gateway accepts and applies to nothing.
    const declaredPublic = new Set(PUBLIC_ROUTES.map((r) => `${r.httpMethod} ${r.resourcePath}`));
    const createdPublic = api.methods
      .filter((m) => m.httpMethod !== "OPTIONS" && isPublicPath(m.resource.path))
      .map((m) => `${m.httpMethod} ${m.resource.path}`);
    for (const method of createdPublic) {
      if (!declaredPublic.has(method)) {
        throw new Error(
          `Public route "${method}" is not in PUBLIC_ROUTES, so it has no throttle ` +
            "and no alarm. Add it there (or do not expose it).",
        );
      }
    }
    if (createdPublic.length !== declaredPublic.size) {
      throw new Error(
        `PUBLIC_ROUTES declares ${declaredPublic.size} public routes but ` +
          `${createdPublic.length} were created. Every declared route must exist, or ` +
          "its throttle silently applies to nothing: API Gateway accepts a " +
          "MethodSetting for a path that is not there.",
      );
    }

    // ----------------------------------------------------------------------
    // Outputs + what the monitoring stack consumes
    // ----------------------------------------------------------------------
    this.refs = {
      region: this.region,
      alarmEmail: myEmail,
      postingsTableName: NAMES.postingsTable,
      cronFunctionName: NAMES.cronFn,
      worklistFunctionName: NAMES.worklistFn,
      publicFunctionName: NAMES.publicFn,
      briefingFunctionName: NAMES.briefingFn,
      apiName: NAMES.api,
      apiStageName: NAMES.apiStage,
      publicRoutes: PUBLIC_ROUTES,
      publicThrottle: PUBLIC_THROTTLE,
      // The literal name, not `cronLogGroup.logGroupName` — that getter returns a
      // CloudFormation Ref, and handing a token to the other stack is exactly the
      // Export/ImportValue coupling this design avoids. Verified by asserting the
      // monitoring template contains no Fn::ImportValue.
      cronLogGroupName: logGroupName(NAMES.cronFn),
      worklistTimeoutSeconds: worklistTimeout.toSeconds(),
    };

    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });
    // The URL the published page fetches. Emitted because it is the one value
    // `tools/ui/build_ui.py` needs baked into index.html for the page to be live
    // rather than a snapshot, and reading it out of the stack beats retyping it.
    new cdk.CfnOutput(this, "PublicApiUrl", {
      value: api.urlForPath("/public"),
      description:
        "Unauthenticated read base URL: /worklist, /worklist/{id}, /internships, " +
        `/excluded. Throttled to ${PUBLIC_THROTTLE.ratePerSecond} req/s per route ` +
        `(burst ${PUBLIC_THROTTLE.burst}).`,
    });
    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, "GmailSecretName", { value: NAMES.gmailSecret });
    new cdk.CfnOutput(this, "PostingsTableName", { value: postingsTable.tableName });
    new cdk.CfnOutput(this, "InterpreterKeyParameter", {
      value: PARAMS.interpreter,
      description:
        "Create as a SecureString to enable the level interpreter (optional): " +
        `aws ssm put-parameter --name ${PARAMS.interpreter} --type SecureString --value <key>`,
    });
  }
}
