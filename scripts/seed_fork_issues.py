#!/usr/bin/env python3
"""One-shot: scan the configured GITHUB_REPO's dependency manifests against
OSV.dev and file real GitHub issues for whatever it finds. This is the same
code path the scheduled background job runs — this script just runs it once,
synchronously, so you can see exactly what got filed before wiring up the
webhook/scheduler.

Usage:
    GITHUB_REPO=youruser/superset GITHUB_TOKEN=$(gh auth token) python scripts/seed_fork_issues.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scan_job import run_scan  # noqa: E402

if __name__ == "__main__":
    result = run_scan()
    print(json.dumps(result, indent=2))
