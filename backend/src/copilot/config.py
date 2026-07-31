"""Runtime configuration, loaded from the environment via pydantic-settings.

One typed Settings object instead of scattered ``os.environ`` reads. In the cloud
these come from Lambda env vars; locally from a ``.env`` file.

**This repository is public.** Nothing personal is hardcoded here — the résumé
template, the tailoring prompts, and the application answer library all live under
``private_dir``, which is gitignored. ``private.example/`` holds placeholder
versions so the code runs for anyone who clones it.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root, derived from this file's location (src/copilot/config.py).
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    # --- storage / AWS ---
    table_name: str = "career-copilot"
    aws_region: str = "us-east-1"

    #: DynamoDB table holding the v2 posting corpus. **Empty means "use SQLite"** —
    #: a deliberate default so a clone of this public repo runs locally against a
    #: file with no AWS account. This is a *different* table from ``table_name``:
    #: that one is the v1 briefing store with an incompatible data model, and
    #: pointing both at one name would let a briefing item and a posting collide.
    postings_table_name: str = ""

    # --- identity (set via env or SSM; never committed) ---
    owner_user_id: str = ""  # Cognito sub the cron writes the briefing under
    my_email: str = ""

    # --- secret ids resolved at runtime in the cloud ---
    #: A Secrets Manager secret id: the Gmail grant is a JSON document, which has no
    #: sane single-value encoding, so this tier is secret-store-or-nothing.
    gmail_secret_id: str = "career-copilot/gmail"
    #: An **SSM Parameter Store path**, not a Secrets Manager id. The two API keys
    #: are single opaque strings, and a SecureString parameter is the cheaper store
    #: for that shape; ``AwsSecrets.api_key`` reads parameters and
    #: ``AwsSecrets.secret_json`` reads secrets, so the store follows from the shape.
    #:
    #: These defaults are the *same strings the stack sets* as ``COPILOT_LLM_SECRET_ID``
    #: and ``COPILOT_INTERPRETER_SECRET_ID``. They used to be ``career-copilot/llm``
    #: and ``career-copilot/interpreter`` — Secrets Manager-shaped names, overridden
    #: in the cloud by the env vars and therefore harmless there, but locally they
    #: named a parameter that will never exist and resolved to ``""`` forever. A
    #: default that is wrong everywhere the env var is absent is a default that
    #: teaches the wrong name; ``tests/test_handlers.py`` pins these against the
    #: paths in ``infra/lib/career-copilot-stack.ts``.
    llm_secret_id: str = "/career-copilot/llm-api-key"
    #: Kept separate from ``llm_secret_id`` on purpose. That one holds the reply
    #: drafter's credentials for a different provider; handing them to the
    #: interpreter's client would authenticate against the wrong API and fail in a
    #: way that looks like a bad key rather than a mis-wiring.
    interpreter_secret_id: str = "/career-copilot/interpreter-api-key"

    # --- direct keys (local dev / tests only; prefer secrets in the cloud) ---
    llm_api_key: str = ""
    #: Absent is a supported state, not an error: the interpreter is the optional
    #: tier that reads a level out of an ambiguous description, and every caller
    #: falls back to the rule verdict when it returns nothing.
    interpreter_api_key: str = ""

    # --- private content (gitignored; see private.example/) ---
    private_dir: Path = Field(
        default=REPO_ROOT / "private",
        description="Résumé template, tailoring prompts, and the answer library.",
    )

    # --- job supply (ATS watchlist path) ---
    watchlist_path: Path = Field(default=REPO_ROOT / "data" / "watchlist.json")
    postings_db_path: Path = Field(default=REPO_ROOT / "data" / "postings.db")
    search_text: str = "software engineer"
    fetch_workers: int = Field(default=6, ge=1, le=16)

    #: How many new roles reach the daily briefing. A cap on the *digest*, never on
    #: the corpus: the worklist API serves every applicable role, and the run
    #: summary reports the full ``kept`` count so this cap cannot read as supply.
    max_jobs: int = Field(default=8, ge=1, le=50)

    # --- résumé build (local only — Lambda has no browser) ---
    resume_variant: str = "software-engineering"

    @property
    def prompts_dir(self) -> Path:
        return self.private_dir / "prompts"

    @property
    def profile_path(self) -> Path:
        return self.private_dir / "profile.json"

    @property
    def answers_path(self) -> Path:
        return self.private_dir / "answers.json"

    @property
    def resume_dir(self) -> Path:
        return self.private_dir / "resume"

    def missing_private_files(self) -> list[Path]:
        """Which private files a feature needs but does not have yet.

        Returned rather than raised so the job-fetching half of the product keeps
        working for someone who has cloned the repo and not filled in `private/`.
        """
        return [p for p in (self.profile_path, self.answers_path) if not p.exists()]


def load_settings() -> Settings:
    """Build Settings from the environment. Kept as a function for easy overriding in tests."""
    return Settings()
