#!/usr/bin/env bash
# Push local credentials into AWS, after `cdk deploy`.
#
# What this replaced: it seeded `career-copilot/anthropic` and `career-copilot/apify`
# from ANTHROPIC_API_KEY / APIFY_TOKEN. No v2 code reads either secret, the v2 stack
# *deletes* both, and there is no Apify adapter at all — so this script's two main
# actions wrote keys nothing would ever fetch, and reported success.
#
# The two API keys now live in **SSM Parameter Store as SecureString**, not Secrets
# Manager. CloudFormation cannot create a SecureString, and a String parameter or a
# Lambda env var would put the key in the synthesised template — so the stack passes
# the parameter *name* and grants a read, and the parameter is created here. Neither
# has to exist for the stack to deploy; a missing key degrades to "no result".
#
# Gmail stays in Secrets Manager: it is the one credential with a rotation lifecycle
# (a refresh token), and the stack creates that secret empty.
#
# ⚠️  Nothing in `backend/src/` fetches any of these yet. `adapters/ssm_secrets.py`
#     does not exist, so the only credential path that works today is a
#     `COPILOT_*_API_KEY` environment variable. Seeding is still worth doing before
#     the runtime half lands — but do not expect the deployed cron to pick it up.
#
# Usage: AWS_PROFILE=<personal-921888034384> ./scripts/seed-secrets.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env — copy .env.example to .env and fill it in."; exit 1; }
set -a; . ./.env; set +a

param() {
  aws ssm put-parameter --name "$1" --type SecureString --value "$2" --overwrite >/dev/null &&
    echo "  ✓ ssm $1"
}
secret() {
  aws secretsmanager put-secret-value --secret-id "$1" --secret-string "$2" >/dev/null &&
    echo "  ✓ secret $1"
}

echo "Seeding credentials into $(aws sts get-caller-identity --query Account --output text)..."

# Names must match the stack's `INTERPRETER_KEY_PARAM` / `LLM_KEY_PARAM`.
if [ -n "${COPILOT_INTERPRETER_API_KEY:-}" ]; then
  param "/career-copilot/interpreter-api-key" "$COPILOT_INTERPRETER_API_KEY"
else
  echo "  - skip interpreter key (COPILOT_INTERPRETER_API_KEY unset)"
fi

if [ -n "${COPILOT_LLM_API_KEY:-}" ]; then
  param "/career-copilot/llm-api-key" "$COPILOT_LLM_API_KEY"
else
  echo "  - skip reply-drafting key (COPILOT_LLM_API_KEY unset)"
fi

if [ -f credentials.json ] && [ -f token.json ]; then
  gmail_json=$(python3 -c 'import json; print(json.dumps({"credentials":json.load(open("credentials.json")),"token":json.load(open("token.json"))}))')
  secret "career-copilot/gmail" "$gmail_json"
else
  echo "  - skip gmail (need credentials.json + token.json; authorize locally first)"
fi

echo "Done."
