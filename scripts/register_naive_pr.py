#!/usr/bin/env python3
"""Registers a naive-bump control PR (opened by hand or by a script that only
edits the pinned version, with no test run) into the audit DB so the
dashboard can show it side by side with the Devin-generated PR for the same
finding.

Usage:
    python scripts/register_naive_pr.py <finding_id> <package> <pr_url> [issue_number]

Example:
    python scripts/register_naive_pr.py PyPI:flask:GHSA-68rp-wp8r-4726 flask \\
        https://github.com/danielgallen/superset/pull/6 2
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.orchestrator import record_naive_pr  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    finding_id, package, pr_url = sys.argv[1], sys.argv[2], sys.argv[3]
    issue_number = int(sys.argv[4]) if len(sys.argv) > 4 else None
    result = record_naive_pr(finding_id, package, pr_url, issue_number, note="version-bump only, no tests run")
    print(result)
