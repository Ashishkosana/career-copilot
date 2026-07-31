#!/usr/bin/env bash
# Run the daily job now, instead of waiting for 14:00 UTC, and tail it.
#
# Why this exists: the raw command is long enough to wrap when pasted, and the
# wrap ate the space before the outfile — `--region us-east-1/tmp/cron.json` —
# which aws then reported as "the following arguments are required: outfile",
# naming a symptom two arguments away from the cause. Same failure that made the
# deploy command unpasteable twice. See deploy.sh.
#
# The region is dropped on purpose: the `personal` profile already sets
# us-east-1, so passing it again is one more token to mangle for no gain.
#
# Usage:
#   ./sweep.sh          # invoke, then follow the log until the run ends
#   ./sweep.sh --now    # invoke and return immediately
set -euo pipefail

# Pinned, not inherited — this shell exports AWS_PROFILE for Crewtron work, and
# the wrong account here would invoke a function that does not exist (harmless)
# or, worse, a same-named one that does. deploy.sh explains this at length.
export AWS_PROFILE="${CAREER_COPILOT_AWS_PROFILE:-personal}"
readonly EXPECTED_ACCOUNT=921888034384
readonly FUNCTION=career-copilot-cron

account="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$account" != "$EXPECTED_ACCOUNT" ]]; then
  echo "Refusing to invoke: AWS_PROFILE=$AWS_PROFILE is account $account," >&2
  echo "not $EXPECTED_ACCOUNT." >&2
  exit 1
fi

out="$(mktemp -t career-copilot-cron)"
echo "Invoking $FUNCTION in $account ..."
aws lambda invoke --function-name "$FUNCTION" --invocation-type Event "$out" >/dev/null
echo "Accepted. The sweep runs in the background for roughly 10 minutes:"
echo "  ~426s fetching 818 boards, ~72s screening, then publishing the view."
rm -f "$out"

if [[ "${1:-}" == "--now" ]]; then
  echo "Follow it with:  aws logs tail /aws/lambda/$FUNCTION --follow --profile $AWS_PROFILE"
  exit 0
fi

echo
echo "Tailing the log. Ctrl-C to stop watching — the run continues regardless."
echo "Expect inbox_fetch_failed: Gmail has no credentials yet and that is contained."
echo
# --since 1m so the tail does not replay a previous run and read as this one.
exec aws logs tail "/aws/lambda/$FUNCTION" --follow --since 1m
