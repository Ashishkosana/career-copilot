import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import { CareerCopilotRefs } from "./career-copilot-stack";

/**
 * Alarms for the career-copilot stack.
 *
 * The alarm that justifies this file existing is {@link MonitoringStack} →
 * *PostingsFetchedTooLow*. Every other alarm here catches a loud failure: a
 * Lambda that raised, a function that was throttled, a table that rejected a
 * write. Those get noticed eventually. The failure this system actually suffered
 * was the quiet one — the supply half read a SQLite database that existed on no
 * machine, fell through to a bundled 4-row fixture, and reported success every
 * single morning for weeks. Nothing errored. Nothing was throttled. The daily
 * email arrived on time with invented companies in it.
 *
 * So the fetch counts are published as metrics and alarmed on *magnitude*: a run
 * that fetches 40 postings instead of 25,000 is a broken scrape, and it has to
 * page someone even though every AWS-level signal is green.
 *
 * ## Coupling
 *
 * This stack takes plain strings, never constructs or tokens from the app stack.
 * A token would compile into a CloudFormation Export/ImportValue pair, and from
 * then on the app stack cannot rename or replace an exported resource while this
 * stack references it — a monitoring stack that can block a product deploy is
 * worse than no monitoring stack. Alarms only ever needed dimension *values*,
 * and those are fixed physical names (see `NAMES`). The one ordering constraint
 * — the cron log group must exist before a metric filter can attach to it — is
 * expressed as an explicit stack dependency in bin/app.ts.
 */
export interface MonitoringStackProps extends cdk.StackProps {
  readonly refs: CareerCopilotRefs;
}

/** Custom namespace for the metrics extracted out of the cron's own log line. */
const NAMESPACE = "CareerCopilot";

/**
 * Measured on real sweeps. Two observations, weeks apart, because the spread is
 * the point: the watchlist is edited by hand and the corpus tracks it.
 *
 *   2026-07-06  25,294 postings /   404 boards /   880 eligible
 *   2026-07-30  48,203 postings /   819 boards / 2,698 eligible  (814 ok, 5 failed)
 *
 * `boards` is the *current* watchlist size, because `MAX_FAILED_SOURCES` has to
 * track it to keep meaning "the share at which the service degrades". The two
 * floors below are deliberately set against the *lower* observation, so growth
 * can never make them fire: the question an alarm answers is "did the scrape
 * break", not "was today quieter than yesterday", and an alarm that cries about
 * normal variance gets muted — which is how the fixture bug survives a second time.
 *
 * If `data/watchlist.json` is expanded again, update `boards`. Nothing breaks if
 * you forget; the failed-board alarm just gets tighter than the code's own rule.
 */
const MEASURED = {
  fetched: 25_294,
  kept: 880,
  boards: 819,
} as const;

/** ~80% of the (smaller) measured corpus gone. At or below this, the fetch broke. */
const MIN_PLAUSIBLE_FETCHED = 5_000;
/** ~89% of the (smaller) measured eligible worklist gone, with the gates unchanged. */
const MIN_PLAUSIBLE_KEPT = 100;
/**
 * A quarter of the watchlist failing is where `DailyBriefingService` itself stops
 * believing the fetch and refuses to close anything (`MAX_FAILED_SOURCE_SHARE`).
 * Derived from the same share so the alarm fires on exactly the condition the code
 * already degrades on, rather than on a number that was true once.
 */
const MAX_FAILED_SOURCES = Math.floor(MEASURED.boards * 0.25);

export class MonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);
    const refs = props.refs;

    // ----------------------------------------------------------------------
    // Where alarms go. Email, because this is a one-person system and the
    // briefing it protects is already an email. AWS sends a confirmation link on
    // first deploy; until it is clicked the subscription is pending and alarms
    // notify nobody, so confirm it before treating this as covered.
    // ----------------------------------------------------------------------
    const topic = new sns.Topic(this, "AlarmTopic", {
      topicName: "career-copilot-alarms",
      displayName: "career-copilot alarms",
    });
    topic.addSubscription(new subscriptions.EmailSubscription(refs.alarmEmail));
    const notify = new actions.SnsAction(topic);

    const alarm = (
      id: string,
      props: {
        metric: cloudwatch.IMetric;
        threshold: number;
        comparisonOperator: cloudwatch.ComparisonOperator;
        alarmDescription: string;
        treatMissingData: cloudwatch.TreatMissingData;
        evaluationPeriods?: number;
        datapointsToAlarm?: number;
      },
    ): cloudwatch.Alarm => {
      const created = new cloudwatch.Alarm(this, id, {
        alarmName: `career-copilot-${id}`,
        evaluationPeriods: 1,
        ...props,
      });
      created.addAlarmAction(notify);
      // Notified on recovery too: an alarm nobody sees clear reads as an
      // unresolved incident and trains you to ignore the next one.
      created.addOkAction(notify);
      return created;
    };

    // ----------------------------------------------------------------------
    // The supply metrics, extracted from the cron's structured log line.
    //
    // `handlers/cron.py` logs one JSON object per run — `{"message":
    // "cron_complete", "fetched": …, "kept": …, "sources_failed": …}` — because
    // every count being present on every run is what makes shrinkage visible.
    // A metric filter turns that line into numbers CloudWatch can alarm on, at
    // no extra instrumentation and no extra code path that could itself break.
    //
    // The numeric comparison in each pattern is doing real work: a JSON filter
    // only matches an event that *has* the field, so a run whose summary shape
    // changed stops publishing rather than publishing a wrong value — and the
    // BREACHING treatment below turns that silence into an alarm.
    // ----------------------------------------------------------------------
    // NOT wired yet, deliberately: the cron also publishes `inbox_ok`, and
    //   { $.message = "cron_complete" && $.inbox_ok IS FALSE }  -> metricValue "1"
    // is the natural fourth filter. Still left out, for a narrower reason than
    // before: `adapters/ssm_secrets.py` can now read the Gmail document, but
    // `handlers/cron.build_service` does not pass the port to `GmailMailbox` yet, and
    // the secret CloudFormation creates is an empty placeholder until it is seeded
    // out of band. So the alarm would be red from the first invocation and stay red —
    // and a permanently-red alarm gets muted, taking the useful ones with it. Add it
    // in the same change that wires the port and seeds the secret, not before.
    const cronLogGroup = logs.LogGroup.fromLogGroupName(
      this,
      "CronLogGroup",
      refs.cronLogGroupName,
    );

    const supplyMetric = (
      id: string,
      metricName: string,
      field: string,
      statistic: string,
    ): cloudwatch.Metric =>
      new logs.MetricFilter(this, id, {
        logGroup: cronLogGroup,
        filterName: `career-copilot-${metricName}`,
        metricNamespace: NAMESPACE,
        metricName,
        filterPattern: logs.FilterPattern.literal(
          `{ $.message = "cron_complete" && $.${field} >= 0 }`,
        ),
        metricValue: `$.${field}`,
        // No defaultValue on purpose: a day with no run must produce *no
        // datapoint*, so `treatMissingData: BREACHING` can catch "the cron
        // stopped logging" with the same alarm that catches "the cron fetched
        // almost nothing". A default of 0 would work too, but it would also
        // report a 0 for a run that never happened, which is a different fault.
      }).metric({
        // One run a day, so the period has to span a day or most windows are
        // empty. MINIMUM rather than SUM so a manual re-invocation cannot mask a
        // bad scheduled run by adding to it.
        statistic,
        period: cdk.Duration.days(1),
      });

    alarm("PostingsFetchedTooLow", {
      metric: supplyMetric("FetchedFilter", "PostingsFetched", "fetched", "Minimum"),
      threshold: MIN_PLAUSIBLE_FETCHED,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      // Missing data is the whole point: no datapoint means the run did not
      // finish, or stopped emitting the summary. Both are this alarm's business.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        `A daily sweep fetched fewer than ${MIN_PLAUSIBLE_FETCHED} postings ` +
        `(a healthy run is ${MEASURED.fetched} or more), or published no count ` +
        "at all. " +
        "This is the alarm for a silently broken scrape — the failure mode that " +
        "let a 4-row fixture pass as real supply for weeks with every AWS metric " +
        "green. Check `failed_sources` in the cron log before assuming the market " +
        "went quiet.",
    });

    alarm("EligibleWorklistTooSmall", {
      metric: supplyMetric("KeptFilter", "EligibleKept", "kept", "Minimum"),
      threshold: MIN_PLAUSIBLE_KEPT,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        `Fewer than ${MIN_PLAUSIBLE_KEPT} postings survived the gates ` +
        `(a healthy run keeps at least ${MEASURED.kept} of ${MEASURED.fetched}). ` +
        "Separate from PostingsFetchedTooLow because it catches the other half: a " +
        "fetch that worked and a *gate* that started rejecting everything — an " +
        "over-broad exclusion pattern looks exactly like a quiet market.",
    });

    alarm("TooManyFailedBoards", {
      metric: supplyMetric("FailedSourcesFilter", "FailedSources", "sources_failed", "Maximum"),
      threshold: MAX_FAILED_SOURCES,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      // Absence is already covered, twice, by the two alarms above; treating it
      // as breaching here would just triple one page.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        `More than ${MAX_FAILED_SOURCES} of ${MEASURED.boards} boards failed ` +
        "(a healthy sweep loses a handful — 5 on 2026-07-30). " +
        "That is the share at which the service stops trusting the fetch and " +
        "refuses to close missing postings, so the corpus is now stale rather " +
        "than wrong — usually one ATS changing its public endpoint, not 100 " +
        "companies going down together.",
    });

    // ----------------------------------------------------------------------
    // The cron itself
    // ----------------------------------------------------------------------
    const lambdaMetric = (
      functionName: string,
      metricName: string,
      statistic: string,
      period: cdk.Duration,
    ): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName,
        dimensionsMap: { FunctionName: functionName },
        statistic,
        period,
      });

    alarm("CronFailed", {
      metric: lambdaMetric(refs.cronFunctionName, "Errors", "Sum", cdk.Duration.hours(1)),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "The daily run raised or timed out. The schedule is configured with " +
        "retryAttempts: 0 — because a retry re-sends the briefing email and " +
        "re-creates reply drafts — so this alarm is the only notice that today's " +
        "briefing did not happen.",
    });

    alarm("CronDidNotRun", {
      metric: lambdaMetric(
        refs.cronFunctionName,
        "Invocations",
        "Sum",
        // 1 day is CloudWatch's maximum alarm period, and the schedule is daily,
        // so this is the smallest window that always contains exactly one run.
        cdk.Duration.days(1),
      ),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        "A whole day passed with no invocation of the cron. Errors cannot catch " +
        "this: a disabled EventBridge rule, a removed target, or a permission " +
        "that stopped allowing the invoke all produce zero errors and zero runs. " +
        "The briefing just stops arriving.",
    });

    // ----------------------------------------------------------------------
    // The read APIs. Combined into one alarm per failure kind with a math
    // expression: three separate Errors alarms would be three line items and
    // three pages saying the same thing, and the function name is in the metric
    // anyway once you open the alarm.
    // ----------------------------------------------------------------------
    const sumAcross = (
      id: string,
      metricName: string,
      functionNames: readonly string[],
      period: cdk.Duration,
    ): cloudwatch.MathExpression => {
      const using: Record<string, cloudwatch.IMetric> = {};
      const ids = functionNames.map((name, index) => {
        const key = `m${index}`;
        using[key] = lambdaMetric(name, metricName, "Sum", period);
        return key;
      });
      return new cloudwatch.MathExpression({
        expression: ids.join(" + "),
        usingMetrics: using,
        label: `${id} (${metricName})`,
        period,
      });
    };

    const readFunctions = [refs.worklistFunctionName, refs.briefingFunctionName] as const;
    // The public function is deliberately absent from both lists below, and it is the
    // one exclusion worth stating. Its errors get their own alarm because "the
    // published page is broken" and "the owner's app is broken" are different
    // incidents with different urgency. Its *throttles* especially: it is the only
    // function with reserved concurrency, so a throttle there is a designed brake
    // engaging under load, while a throttle on any of these three is an account-level
    // capacity problem. Folding them together would make a busy day on a public page
    // page him about his cron.
    const allFunctions = [refs.cronFunctionName, ...readFunctions] as const;

    alarm("ReadApiErrors", {
      metric: sumAcross("ReadApi", "Errors", readFunctions, cdk.Duration.minutes(5)),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "A worklist or briefing read raised. Note that the handlers turn an " +
        "unavailable store into a 503 *response* rather than an exception, so " +
        "this metric is unhandled faults only — pair it with ApiGateway5xx.",
    });

    alarm("LambdaThrottled", {
      metric: sumAcross("AllFunctions", "Throttles", allFunctions, cdk.Duration.minutes(5)),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "A function was refused a container. On an account with the default " +
        "1,000 concurrent executions this should be impossible for a one-user " +
        "system, so a throttle here means something else in the account is " +
        "consuming the limit — and a throttled *cron* is a briefing that never " +
        "ran, with no error to show for it.",
    });

    alarm("WorklistNearTimeout", {
      metric: lambdaMetric(
        refs.worklistFunctionName,
        "Duration",
        "p99",
        cdk.Duration.minutes(15),
      ),
      // 85% of the ceiling: past that, a slightly larger corpus turns a slow
      // response into a 504.
      threshold: Math.floor(refs.worklistTimeoutSeconds * 1000 * 0.85),
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        `p99 duration is within 15% of the ${refs.worklistTimeoutSeconds}s ` +
        "integration ceiling. A cold container screens every open posting before " +
        "it can answer, so this alarm tracks the corpus growing past what one " +
        "request can do — the fix is a stored screening pass, not a bigger " +
        "timeout, because API Gateway REST will not wait longer than 29s.",
    });

    alarm("ApiGateway5xx", {
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApiGateway",
        metricName: "5XXError",
        dimensionsMap: { ApiName: refs.apiName },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "API Gateway returned 5xx. Catches the failures that never reach a " +
        "handler — an integration timeout, a bad deployment, a missing invoke " +
        "permission — which the Lambda Errors metric cannot see.",
    });

    // ----------------------------------------------------------------------
    // The public, unauthenticated surface.
    //
    // A different failure profile from every alarm above, because for the first time
    // the caller is not the owner. Two things can go wrong that the existing alarms
    // cannot see:
    //
    //   1. the published page is broken *for visitors* — and nobody tells you, because
    //      a visitor who gets a 502 leaves rather than files a bug;
    //   2. the endpoint is being abused, which on an open route is a bill.
    //
    // Both are measured per method, which needs `metricsEnabled` on those methods —
    // the stack sets it explicitly on each public method for exactly this reason.
    // The dimension values come from `refs.publicRoutes`, the same list the throttles
    // are built from, so an alarm cannot end up watching a path that does not exist.
    //
    // treatMissingData is NOT_BREACHING on all four, which is worth defending given
    // the convention elsewhere in this file: absence here is *not* the failure. A
    // portfolio page legitimately gets zero requests at 3am, and an alarm that fires
    // every quiet night is an alarm that gets muted. "The public route is broken for
    // everyone" is a real gap this leaves, and the honest fix is a synthetic canary
    // hitting /public/worklist on a schedule — not a BREACHING alarm on organic
    // traffic that would cry wolf nightly to catch it.
    // ----------------------------------------------------------------------

    /**
     * One API Gateway metric per public (resource, method), registered under
     * `<prefix><i>` and returned as ids for a flat math expression. Flat, not nested:
     * a MathExpression whose `usingMetrics` contains another MathExpression is a
     * shape CloudWatch renders differently, and one sum of eight metrics is easier to
     * read in the console than a tree.
     */
    const publicRouteMetricIds = (
      metricName: string,
      statistic: string,
      prefix: string,
      period: cdk.Duration,
      using: Record<string, cloudwatch.IMetric>,
    ): string[] =>
      refs.publicRoutes.map((route, index) => {
        const key = `${prefix}${index}`;
        using[key] = new cloudwatch.Metric({
          namespace: "AWS/ApiGateway",
          metricName,
          dimensionsMap: {
            ApiName: refs.apiName,
            Stage: refs.apiStageName,
            Resource: route.resourcePath,
            Method: route.httpMethod,
          },
          statistic,
          period,
        });
        return key;
      });

    const publicPeriod = cdk.Duration.minutes(5);

    const serverErrorMetrics: Record<string, cloudwatch.IMetric> = {};
    alarm("PublicApi5xx", {
      metric: new cloudwatch.MathExpression({
        expression: publicRouteMetricIds(
          "5XXError",
          "Sum",
          "e",
          publicPeriod,
          serverErrorMetrics,
        ).join(" + "),
        usingMetrics: serverErrorMetrics,
        label: "Public routes (5XXError)",
        period: publicPeriod,
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "An anonymous visitor to the published page got a 5xx. Separate from " +
        "ApiGateway5xx, which is API-wide: this one says the *public* half is what " +
        "broke, and it is the only signal there is — a visitor who sees a broken " +
        "page leaves rather than reports it. Two causes to check first: the Lambda " +
        "asset missing `copilot/handlers/public_api.py` (an import error, 502 on " +
        "every request), and the projection failing closed on a renamed upstream " +
        "field, which answers 500 `public_projection_failed` on purpose rather than " +
        "publishing a half-projected body.",
    });

    const rejectedMetrics: Record<string, cloudwatch.IMetric> = {};
    const rejectedIds = publicRouteMetricIds("4XXError", "Sum", "r", publicPeriod, rejectedMetrics);
    const requestIds = publicRouteMetricIds(
      "Count",
      // SampleCount, not Sum: for the `Count` metric each datapoint *is* a request,
      // so Sum would be counting the requests' values rather than the requests.
      "SampleCount",
      "n",
      publicPeriod,
      rejectedMetrics,
    );
    alarm("PublicApiRejectingRequests", {
      metric: new cloudwatch.MathExpression({
        // Ratio, not a raw count: the routes are throttled per method, so the
        // interesting question is what *share* of public traffic is being turned
        // away. A count would alarm on a popular day and stay quiet on a broken one.
        // Zero requests yields no datapoint (division by zero), which NOT_BREACHING
        // then reads as "quiet", correctly.
        expression: `100 * (${rejectedIds.join(" + ")}) / (${requestIds.join(" + ")})`,
        usingMetrics: rejectedMetrics,
        label: "Public routes: 4xx share of requests (%)",
        period: publicPeriod,
      }),
      threshold: 25,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      // Sustained, not instantaneous: a burst of 429s while the token bucket refills
      // is the throttle doing its job, and paging on one window would make the cost
      // brake and the alarm contradict each other.
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "More than 25% of public requests were rejected for 10 of the last 15 " +
        `minutes. The per-route throttle is ${refs.publicThrottle.ratePerSecond} req/s ` +
        `with a burst of ${refs.publicThrottle.burst}, so sustained rejection means ` +
        "either something is scraping the endpoint hard enough to be shed (check the " +
        "bill, then consider a WAF rate rule) or the page itself is asking for a " +
        "route that does not exist. Note the metric is 4XXError, not a throttle " +
        "count: REST APIs publish no 429-only metric, so a 404 storm from a scanner " +
        "and a real throttle look the same here — the request count and the paths in " +
        "the API Gateway console are what tell them apart.",
    });

    alarm("PublicFunctionErrors", {
      metric: lambdaMetric(refs.publicFunctionName, "Errors", "Sum", publicPeriod),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "The public read function raised. Like ReadApiErrors this is unhandled " +
        "faults only — the handler turns an unavailable store into a 503 *response* " +
        "and a projection failure into a 500 *response*, so a raise here means " +
        "something outside the routing table failed: config, the store client, or " +
        "the asset. Kept separate from ReadApiErrors so the public page and the " +
        "owner's app are never the same incident.",
    });

    alarm("PublicFunctionThrottled", {
      metric: lambdaMetric(refs.publicFunctionName, "Throttles", "Sum", publicPeriod),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      // Same reasoning as the rejection-rate alarm: the reserved concurrency exists
      // to shed load, so shedding once is the design working. Two windows out of
      // three is the design being *exceeded*.
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "The public function hit its reserved concurrency in 2 of the last 3 " +
        "windows. This is the one throttle in the system that is *self-inflicted on " +
        "purpose* — the reservation is what stops an open endpoint from eating the " +
        "account's concurrency and taking the daily cron with it. But a throttled " +
        "invoke reaches the visitor as a 5xx, not a 429, so sustained throttling " +
        "means the page is failing for people: either raise the reservation, or find " +
        "out why requests got slow enough to need that many containers (the " +
        "cold-start screening pass is the usual answer).",
    });

    // ----------------------------------------------------------------------
    // The postings table
    // ----------------------------------------------------------------------
    alarm("PostingsTableThrottled", {
      metric: new cloudwatch.Metric({
        namespace: "AWS/DynamoDB",
        metricName: "ThrottledRequests",
        dimensionsMap: { TableName: refs.postingsTableName },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      // On-demand throttling is not free of consequence but it is not instantly
      // fatal either: the adapter runs adaptive retries. A handful during the
      // burst at the start of a sync is tolerable; sustained throttling is the
      // hot-partition failure the 16-way id sharding exists to prevent, and it
      // means a fetch is being dropped on the floor.
      threshold: 100,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "The postings table throttled sustained traffic. A first sync writes " +
        "~25k items in one burst; the schema shards `open_pk`/`seen_pk` 16 ways " +
        "over the leading hex digit of the posting id precisely so no single " +
        "partition takes all of it. Sustained throttling means that assumption " +
        "broke — check whether an id prefix stopped being uniform, or whether " +
        "on-demand capacity is still ramping after a long idle period.",
    });

    new cdk.CfnOutput(this, "AlarmTopicArn", { value: topic.topicArn });
    new cdk.CfnOutput(this, "AlarmEmail", {
      value: refs.alarmEmail,
      description: "Confirm the SNS subscription in this inbox or no alarm notifies anyone.",
    });
  }
}
