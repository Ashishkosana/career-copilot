#!/usr/bin/env bash
# Give the `career-copilot-levels` user the only three things it needs.
#
# Why a script for one command: the raw `aws iam put-user-policy` line is ~150
# characters and wrapped on paste every time, splitting `--policy-document` from its
# value. AWS then reports "expected one argument" and zsh separately tries to execute
# the filename, so two errors appear and neither names the cause. Fifth occurrence of
# that in this project — see deploy.sh and sweep.sh for the same fix.
#
# The user is the identity GitHub Actions uses for the `levels` workflow. Its policy
# lets it read postings and write back a seniority level, on one table. It cannot
# delete anything, cannot read the Gmail secret, and cannot touch a Lambda — so a
# leaked key costs at worst some wrong seniority labels.
#
# Usage:  ./grant.sh          apply the policy
#         ./grant.sh --show   print what the user can currently do
set -euo pipefail

export AWS_PROFILE="${CAREER_COPILOT_AWS_PROFILE:-personal}"
readonly EXPECTED_ACCOUNT=921888034384
readonly USER=career-copilot-levels
readonly POLICY=levels

cd "$(dirname "$0")"

account="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$account" != "$EXPECTED_ACCOUNT" ]]; then
  echo "Refusing: AWS_PROFILE=$AWS_PROFILE is account $account, not $EXPECTED_ACCOUNT." >&2
  exit 1
fi

if [[ "${1:-}" == "--show" ]]; then
  echo "Inline policies on $USER:"
  aws iam list-user-policies --user-name "$USER" --query 'PolicyNames' --output text
  aws iam get-user-policy --user-name "$USER" --policy-name "$POLICY" \
    --query 'PolicyDocument.Statement[0].Action' --output text 2>/dev/null \
    || echo "(no '$POLICY' policy attached)"
  exit 0
fi

aws iam put-user-policy \
  --user-name "$USER" \
  --policy-name "$POLICY" \
  --policy-document file://levels-user-policy.json

echo "Applied. $USER can now do:"
aws iam get-user-policy --user-name "$USER" --policy-name "$POLICY" \
  --query 'PolicyDocument.Statement[0].Action' --output text
echo "…on career-copilot-postings only."
