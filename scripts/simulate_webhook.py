#!/usr/bin/env python3
"""Fires a synthetic GitHub `issues.labeled` webhook event at a locally running
instance of the app, signed with the same secret it expects — useful for
demoing/testing the trigger path without exposing a public URL or waiting on
a real GitHub delivery.

It still fetches the *real* issue body from GITHUB_REPO via the GitHub API,
so the Devin prompt built from it is genuine, not synthetic.

Usage:
    python scripts/simulate_webhook.py <issue_number> [--url http://localhost:8080]
"""

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.github_client import GitHubClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_number", type=int)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--label", default=settings.trigger_label)
    args = parser.parse_args()

    gh = GitHubClient()
    issue = gh.get_issue(args.issue_number)

    payload = {
        "action": "labeled",
        "issue": issue,
        "label": {"name": args.label},
        "repository": {"full_name": settings.github_repo},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    resp = httpx.post(
        f"{args.url}/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "simulated-local-delivery",
        },
        timeout=30,
    )
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
