#!/usr/bin/env bash
# Build the Lambda deployment asset WITHOUT Docker.
#
# What changed and why it mattered: this script used to bundle `../src/career_copilot`
# — the v1 package — while every v2 entry point lives in `../backend/src/copilot`.
# The stack's handler strings pointed at the v1 module names to match, so the
# deployed Lambda ran v1 code no matter what was merged into backend/.
#
# Three things go into the asset, and all three are load-bearing:
#
#   1. dependency wheels, installed as manylinux (no Docker, no compiler);
#   2. the `copilot` package itself;
#   3. `data/watchlist.json` — the list of ATS boards to poll.
#
# (3) is not optional. `Settings.watchlist_path` defaults to `REPO_ROOT/data/…`,
# and REPO_ROOT is derived from the *file location* of config.py: under /var/task
# that resolves to `/data/watchlist.json`, which does not exist. `load_watchlist`
# answers a missing file with an empty list, the fan-out then fetches zero boards,
# and the run reports "0 roles today" — the exact shape of the fixture bug this
# rewrite was meant to end. The file is copied here and pointed at explicitly by
# COPILOT_WATCHLIST_PATH in the stack, and this script fails if it is missing.
#
# Run before `cdk synth` / `cdk deploy`: the stack reads infra/build as an asset
# and refuses to synth if the v2 package is not in it.
set -euo pipefail
cd "$(dirname "$0")"

ROOT=..
BACKEND=$ROOT/backend
PKG=$BACKEND/src/copilot
BUILD=build

# Lambda's unzipped ceiling. Warned about rather than enforced, because the fix
# is a judgement call (drop an adapter, or move to a container image).
MAX_UNZIPPED_MB=250

[ -d "$PKG" ] || { echo "ERROR: $PKG not found — expected the v2 package."; exit 1; }
[ -f "$ROOT/data/watchlist.json" ] || {
  echo "ERROR: $ROOT/data/watchlist.json not found."
  echo "       Without it the deployed fetch polls zero boards and reports 0 roles."
  exit 1
}

# A Python that can read pyproject.toml. `python3` on macOS is still the system
# 3.9, which has no tomllib (stdlib from 3.11), so the interpreter is resolved
# rather than assumed — otherwise this script fails on a fresh Mac with an error
# that points at the wrong thing.
PY_BIN=""
for candidate in "$BACKEND/.venv/bin/python3" python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c "import tomllib, pip" >/dev/null 2>&1; then
    PY_BIN=$candidate
    break
  fi
done
[ -n "$PY_BIN" ] || {
  echo "ERROR: no Python >=3.11 with pip found (need tomllib to read pyproject.toml)."
  echo "       Tried: backend/.venv, python3.13, python3.12, python3.11, python3."
  exit 1
}
echo "Using $("$PY_BIN" -V) at $(command -v "$PY_BIN")"

rm -rf "$BUILD" && mkdir -p "$BUILD"

# ---------------------------------------------------------------------------
# 1. Dependencies, derived from backend/pyproject.toml rather than a second list.
#
# A hand-maintained infra/requirements.txt is how a bundle silently drifts from
# the package it is bundling: add pydantic-settings to the project, forget the
# copy here, and the Lambda dies on import with a stack trace that looks nothing
# like the cause. tomllib is stdlib from 3.11, so this costs no dependency.
#
# boto3/botocore are deliberately EXCLUDED: the Python 3.13 Lambda runtime ships
# them, and they are ~50 MB of the unzipped ceiling. Everything this code needs
# from them (`Config(retries={"mode": "adaptive"})`, the DynamoDB resource API,
# batch_writer) has been in botocore for years. If a future adapter needs a
# newer botocore feature, add it back here and say so — do not assume the
# runtime's copy is current.
# ---------------------------------------------------------------------------
REQ=$(mktemp)
trap 'rm -f "$REQ"' EXIT

"$PY_BIN" - "$BACKEND/pyproject.toml" >"$REQ" <<'PY'
import sys
import tomllib

RUNTIME_PROVIDED = {"boto3", "botocore"}


def name_of(spec: str) -> str:
    """Distribution name from a PEP 508 spec, lowercased."""
    for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("_", "-")


with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]

# Runtime deps + the `adapters` extra: the Lambda *is* the adapter layer. `dev`
# (pytest/ruff/mypy) is never bundled.
specs = list(project["dependencies"]) + list(project["optional-dependencies"]["adapters"])
for spec in specs:
    if name_of(spec) not in RUNTIME_PROVIDED:
        print(spec)
PY

echo "Installing deps as manylinux (Python 3.13, x86_64) wheels..."
sed 's/^/  /' "$REQ"
"$PY_BIN" -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --upgrade \
  --target "$BUILD" \
  -r "$REQ" \
  --quiet

# ---------------------------------------------------------------------------
# 2. The package, 3. the watchlist, and (optionally) private content.
# ---------------------------------------------------------------------------
echo "Copying the copilot package..."
cp -R "$PKG" "$BUILD/copilot"

echo "Copying data/watchlist.json..."
mkdir -p "$BUILD/data"
cp "$ROOT/data/watchlist.json" "$BUILD/data/watchlist.json"

# private/ holds personal content used for coverage scoring. It is gitignored, so
# a clone of this public repo has none — and the code already degrades to
# `scoring.available = false` rather than scoring against an empty document.
#
# Only the résumé is copied, not the whole directory. `handlers.worklist_api.
# load_resume_text` is the *only* thing any Lambda reads out of private_dir
# (`resume_dir/<variant>.txt|.md|html/<variant>.html`); `profile.json`,
# `answers.json`, `prompts/` and `letters/` are read exclusively by the local
# résumé-tailoring scripts. Those files carry a phone number, an address and a
# full answer library, and this asset is uploaded to the CDK assets S3 bucket and
# unpacked into every container — so shipping them puts personal data somewhere no
# deployed code can even use it. Same least-privilege reasoning as the IAM policies,
# applied to data instead of actions.
if [ -d "$ROOT/private/resume" ]; then
  mkdir -p "$BUILD/private/resume/html"
  # The three shapes load_resume_text() looks for, and nothing else. `|| true`
  # because only one of them normally exists.
  cp "$ROOT"/private/resume/*.txt "$BUILD/private/resume/" 2>/dev/null || true
  cp "$ROOT"/private/resume/*.md "$BUILD/private/resume/" 2>/dev/null || true
  cp "$ROOT"/private/resume/html/*.html "$BUILD/private/resume/html/" 2>/dev/null || true
  cp "$ROOT"/private/resume/html/*.css "$BUILD/private/resume/html/" 2>/dev/null || true
  if [ -n "$(ls -A "$BUILD/private/resume" 2>/dev/null)" ]; then
    echo "Copied private/resume only — the worklist will report score tiers."
    echo "  bundled: $(cd "$BUILD/private" && find . -type f | sed 's|^\./||' | tr '\n' ' ')"
  else
    echo "private/resume exists but holds no .txt/.md/.html — scoring reports unavailable."
  fi
else
  echo "No private/resume — deploying without a résumé; scoring reports unavailable."
fi

# ---------------------------------------------------------------------------
# Trim, and refuse to ship things that change behaviour in the cloud.
# ---------------------------------------------------------------------------
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
# Console-script shims (httpx, websockets, google-oauthlib-tool …). Nothing in
# Lambda executes them, and /var/task is not on PATH anyway.
rm -rf "$BUILD/bin"

# `Settings` reads a `.env` from the process CWD, which in Lambda is /var/task.
# A stray .env in the asset would silently outrank the env vars the stack sets —
# and could carry a real API key into a deployment artefact. Never ship one.
find "$BUILD" -maxdepth 2 -name ".env*" -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# Assert the asset is what the stack's handler strings claim it is. A stale
# build/ directory from the v1 script is indistinguishable from a good one at
# `cdk deploy` time — the deploy succeeds and the Lambda 500s on import.
#
# One entry per `handler:` string in career-copilot-stack.ts. public_api.py was
# missing from this list while the stack already pointed a function at it, which is
# the exact failure the paragraph above describes and the worst version of it: the
# public route is the one nobody is authenticated to notice is down, so a stale
# asset would have served 502 to every visitor of jobs.ashishkosana.com while the
# authenticated app looked perfectly healthy. Adding a Lambda without adding it
# here is silent; adding it here costs one line.
# ---------------------------------------------------------------------------
for required in copilot/handlers/cron.py copilot/handlers/worklist_api.py \
                copilot/handlers/public_api.py copilot/handlers/api.py \
                copilot/adapters/ssm_secrets.py \
                copilot/config.py data/watchlist.json; do
  [ -e "$BUILD/$required" ] || { echo "ERROR: $BUILD/$required missing."; exit 1; }
done
if [ -d "$BUILD/career_copilot" ]; then
  echo "ERROR: the v1 package career_copilot is in the asset; this build is stale."
  exit 1
fi
# Checked by presence, not by importing: these are *Linux* wheels, so
# `import pydantic` on a Mac would fail on pydantic_core's .so and say nothing
# about whether the asset is correct.
for dist in pydantic pydantic_core pydantic_settings; do
  [ -d "$BUILD/$dist" ] || { echo "ERROR: $dist did not install into the asset."; exit 1; }
done
if ! ls "$BUILD"/pydantic_core/*linux*.so >/dev/null 2>&1; then
  echo "ERROR: pydantic_core has no linux .so — pip resolved a wheel for this Mac."
  exit 1
fi

SIZE_MB=$(du -sm "$BUILD" | cut -f1)
echo "Built asset in infra/$BUILD (${SIZE_MB} MB unzipped)"
if [ "$SIZE_MB" -gt "$MAX_UNZIPPED_MB" ]; then
  echo "WARNING: over Lambda's ${MAX_UNZIPPED_MB} MB unzipped limit — the deploy will fail."
fi
