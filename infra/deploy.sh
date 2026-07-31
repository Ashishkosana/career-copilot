#!/usr/bin/env bash
# Deploy the stacks, without a command line long enough to get mangled.
#
# Why this exists: the required values are passed as `cdk deploy -c key=value`
# pairs, and that command is ~180 characters. Pasted into a terminal it wraps, the
# wrap splits `-c` from its value, and cdk then reports something unrelated —
# "Unknown option(s): --w, --n, --U" — while the real fault is a missing context
# key. That happened twice in a row. A backslash continuation does not help: the
# backslash must be the final character on the line and a pasted trailing space
# silently breaks it, which is how the same command once ran as just `cdk deploy`
# and complained that `--all` was missing.
#
# So the values live here, one per line, and nothing has to survive a paste.
#
# Usage:
#   ./deploy.sh                 # deploy both stacks
#   ./deploy.sh --diff          # show what would change, deploy nothing
#   ./deploy.sh --yes           # skip the IAM approval prompt
set -euo pipefail

# --- the values the stacks refuse to synth without -------------------------
# MY_EMAIL      where the daily briefing and every alarm goes.
# OWNER_USER_ID the Cognito `sub` the briefing is stored under. Read it with:
#   aws cognito-idp list-users --user-pool-id <pool> --profile personal \
#     --query 'Users[].Attributes[?Name==`sub`].Value'
export MY_EMAIL="${MY_EMAIL:-ashishkosana@gmail.com}"
export OWNER_USER_ID="${OWNER_USER_ID:-84d81458-2011-705f-eec4-ccda7dcd1e35}"

# The personal account, not the work one.
#
# Deliberately NOT `${AWS_PROFILE:-personal}`. This shell usually already exports
# AWS_PROFILE for Crewtron work (crewtron-beta → account 425680120934), and a
# default that defers to it means the script quietly aims at the work account —
# which is the exact accident it exists to prevent. The override has its own name,
# so pointing this somewhere else has to be deliberate.
export AWS_PROFILE="${CAREER_COPILOT_AWS_PROFILE:-personal}"
readonly EXPECTED_ACCOUNT=921888034384

cd "$(dirname "$0")"

account="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$account" != "$EXPECTED_ACCOUNT" ]]; then
  echo "Refusing to deploy: AWS_PROFILE=$AWS_PROFILE resolves to account $account," >&2
  echo "but this stack lives in $EXPECTED_ACCOUNT. Deploying to the wrong account" >&2
  echo "would create a second copy of everything and leave it running." >&2
  exit 1
fi

# The Lambda asset is built here, Docker-free, and the stack fails synth if the
# bundle is missing a handler — so build before every deploy rather than trusting
# whatever build/ happens to contain from last time.
./build-lambda.sh

case "${1:-}" in
  --diff) exec npx cdk diff --all ;;
  --yes)  exec npx cdk deploy --all --require-approval never ;;
  "")     exec npx cdk deploy --all ;;
  *)      echo "usage: $0 [--diff|--yes]" >&2; exit 2 ;;
esac
