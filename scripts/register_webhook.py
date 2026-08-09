#!/usr/bin/env python3
"""Registers the `issues` webhook on GITHUB_REPO pointing at a given public URL,
using the same secret configured in .env (GITHUB_WEBHOOK_SECRET). Requires the
`gh` CLI to be authenticated (`gh auth login`).

Usage:
    python scripts/register_webhook.py https://your-public-url.example.com
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    public_url = sys.argv[1].rstrip("/")
    if not settings.github_repo:
        print("GITHUB_REPO is not set in .env", file=sys.stderr)
        sys.exit(1)
    if not settings.github_webhook_secret or settings.github_webhook_secret == "change_me":
        print("Set a real GITHUB_WEBHOOK_SECRET in .env first (openssl rand -hex 32)", file=sys.stderr)
        sys.exit(1)

    cmd = [
        "gh", "api",
        f"repos/{settings.github_repo}/hooks",
        "-X", "POST",
        "-f", "name=web",
        "-f", "active=true",
        "-f", "events[]=issues",
        "-f", f"config[url]={public_url}/webhooks/github",
        "-f", "config[content_type]=json",
        "-f", f"config[secret]={settings.github_webhook_secret}",
    ]
    print("Registering webhook:", public_url + "/webhooks/github", "->", settings.github_repo)
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
