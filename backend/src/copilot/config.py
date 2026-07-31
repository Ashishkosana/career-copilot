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

    # --- identity (set via env or SSM; never committed) ---
    owner_user_id: str = ""  # Cognito sub the cron writes the briefing under
    my_email: str = ""

    # --- secret ids resolved at runtime in the cloud ---
    gmail_secret_id: str = "career-copilot/gmail"
    llm_secret_id: str = "career-copilot/llm"

    # --- direct keys (local dev / tests only; prefer secrets in the cloud) ---
    llm_api_key: str = ""

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

    # --- legacy job engine ---
    # Still read by handlers/cron.py, which runs the v1 briefing path. That path
    # is being replaced by the ATS watchlist above; these go when it does.
    ja_db_path: str = ""
    min_job_score: int = Field(default=40, ge=0, le=100)
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
