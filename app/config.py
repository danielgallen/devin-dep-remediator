"""Environment-driven configuration. No secrets have defaults that work in prod."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    devin_api_key: str = os.environ.get("DEVIN_API_KEY", "")  # service-user token, prefix cog_
    devin_api_base: str = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai/v3")
    devin_org_id: str = os.environ.get("DEVIN_ORG_ID", "")  # optional; auto-resolved from GET /v3/self if unset

    github_token: str = os.environ.get("GITHUB_TOKEN", "")
    github_repo: str = os.environ.get("GITHUB_REPO", "")
    github_webhook_secret: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    trigger_label: str = os.environ.get("DEVIN_TRIGGER_LABEL", "devin-fix")
    in_progress_label: str = os.environ.get("DEVIN_IN_PROGRESS_LABEL", "devin-in-progress")
    done_label: str = os.environ.get("DEVIN_DONE_LABEL", "devin-pr-open")
    failed_label: str = os.environ.get("DEVIN_FAILED_LABEL", "devin-failed")
    blocked_label: str = os.environ.get("DEVIN_BLOCKED_LABEL", "devin-blocked")

    poll_interval_seconds: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    scan_interval_seconds: int = int(os.environ.get("SCAN_INTERVAL_SECONDS", "86400"))

    database_path: str = os.environ.get("DATABASE_PATH", "./data/remediator.db")
    port: int = int(os.environ.get("PORT", "8080"))


settings = Settings()
