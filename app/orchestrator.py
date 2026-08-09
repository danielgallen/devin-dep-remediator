"""Core event-driven logic: issue labeled -> Devin session -> poll -> PR -> close loop.

This module is the actual "automation" — everything else (webhook route,
scheduler, dashboard) is plumbing around it.
"""

import logging
import re
from datetime import datetime, timezone

from app import db
from app.config import settings
from app.devin_client import TERMINAL_STATUSES, DevinClient
from app.github_client import GitHubClient

log = logging.getLogger("orchestrator")

devin = DevinClient()
github = GitHubClient()

_FIELD_RE = re.compile(r"^\*\*(?P<key>[\w /]+):\*\*\s*(?P<value>.+)$", re.MULTILINE)

# A PR that only touches these paths made a version-string edit and nothing
# else — exactly what a naive bump script (or Dependabot) produces. Anything
# beyond this set means real code was diagnosed and changed.
LOCKFILE_PATHS = (
    "requirements/base.in",
    "requirements/base.txt",
    "requirements/development.in",
    "requirements/development.txt",
    "requirements/translations.in",
    "requirements/translations.txt",
    "superset-frontend/package.json",
    "superset-frontend/package-lock.json",
    "pyproject.toml",  # Superset also declares some direct deps here, not just requirements/
)


def is_lockfile_only(files: list[str]) -> bool:
    return bool(files) and all(f in LOCKFILE_PATHS for f in files)


def _parse_issue_fields(body: str) -> dict[str, str]:
    return {m.group("key").strip().lower(): m.group("value").strip() for m in _FIELD_RE.finditer(body or "")}


# JSON Schema (Draft 7) passed as structured_output_schema on session creation,
# with structured_output_required=True. This is what makes "fixed" vs. "blocked"
# a verdict Devin is forced to commit to and justify, not a status we infer from
# whether a PR happened to appear -- see README "Design notes".
REMEDIATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "rationale", "prior_constraint_found"],
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["fixed", "blocked"],
            "description": (
                "'fixed' ONLY if you opened a pull request with a remediation you have positively "
                "verified is safe. 'blocked' if you could not produce a safe fix -- e.g. no patched "
                "version has been released yet, or the fix would reintroduce a failure mode this "
                "codebase has already hit and documented. 'blocked' is a correct, valuable outcome, "
                "not a failure -- do not stretch to force 'fixed' when the evidence doesn't support it."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "2-6 sentences a reviewing engineer can act on: what you found and why it does or doesn't resolve the advisory.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete citations: file:line, quoted comments, advisory text, linked issue/PR numbers/URLs. No unsupported claims.",
        },
        "prior_constraint_found": {
            "type": "boolean",
            "description": (
                "True if you found an existing version ceiling/floor or comment in this repo "
                "(pyproject.toml, requirements/*.in, setup.cfg, etc.) already restricting this "
                "package or a package that depends on it, for a documented reason."
            ),
        },
        "pr_url": {
            "type": ["string", "null"],
            "description": "URL of the opened pull request, or null when outcome is 'blocked'.",
        },
    },
}


def build_prompt(issue: dict) -> str:
    fields = _parse_issue_fields(issue.get("body", ""))
    package = fields.get("package", "unknown")
    current = fields.get("current version", "unknown")
    fixed_raw = fields.get("fixed version", "")
    ecosystem = fields.get("ecosystem", "PyPI")
    vuln_id = fields.get("vuln id", "n/a")
    repo = settings.github_repo

    # The scanner (app/scanner.py:_fixed_version_of) leaves this unset -- and the
    # issue body says "see advisory" -- when OSV has no `fixed` event for this
    # advisory, i.e. upstream hasn't shipped a patch yet. Interpolating that
    # string into "bump to {fixed}" produces a nonsense instruction ("bump
    # paramiko to see advisory"), so branch on it explicitly instead.
    fixed_known = bool(fixed_raw) and fixed_raw.lower() != "see advisory"
    fixed = fixed_raw if fixed_known else None

    lockfile_hint = (
        "requirements/base.in (then regenerate requirements/base.txt and requirements/development.txt "
        "with `pip-compile-multi` per requirements/README.md)"
        if ecosystem.lower() in ("pypi", "python")
        else "superset-frontend/package.json (then update superset-frontend/package-lock.json)"
    )

    target_line = (
        f"Target fixed version: {fixed}"
        if fixed_known
        else "Target fixed version: NOT LISTED by OSV -- you must verify one actually exists (see step 1)."
    )

    return f"""You are remediating a dependency vulnerability in the {repo} repository (a fork of apache/superset).

Advisory: {vuln_id}
Package: {package}
Current pinned version: {current}
{target_line}
Ecosystem: {ecosystem}

This is a real production codebase with real history. Multiple dependency ceilings in this repo
exist *because a past upgrade attempt broke something in production* -- the fix was reverted and a
comment left behind explaining why. Your job is to act like the senior engineer who already knows
that history, not like a bump script. Concretely:

1. Investigate before changing anything:
   a. Confirm a patched version actually exists and is released. If "Target fixed version" above is
      not listed, look up advisory {vuln_id} yourself (osv.dev / the GHSA page). If no fixed release
      exists yet, do not invent one, and do not bump to some other "latest" version and call it done
      -- that doesn't resolve the CVE and isn't what was asked.
   b. Grep `pyproject.toml` and `requirements/*.in` for `{package}` AND for any inline comment near it
      or near packages that depend on it. Superset's pyproject.toml documents several of these
      (e.g. the flask-sqlalchemy pin references PR #42542; the paramiko pin references DSSKey/sshtunnel).
      If you find a comment explaining why a version is capped, that comment is evidence of a real
      prior incident -- read it fully and treat it as authoritative until you've specifically
      investigated whether it still applies.
   c. If a comment references a tracking issue, PR, or discussion number, look it up (`gh issue view`,
      `gh pr view`, or search the repo) to understand the actual failure mode before proceeding.
   d. Grep the codebase for usages of any API/module the target version is known to remove or change
      (check the package's own changelog/release notes for the version range you're crossing), not
      just for `{package}` itself -- transitive breakage (e.g. a different dependency importing a
      module that got removed) is exactly the kind of thing a version-string bump misses.

2. If step 1 surfaces a real blocker -- no patched version exists, or the fix would re-cross a
   boundary this codebase already crossed and reverted, or it breaks another in-tree consumer -- STOP.
   Do not open a pull request. Set outcome="blocked" in your structured output with a rationale
   specific enough that a human doesn't have to redo your research, and cite the evidence (file:line,
   PR/issue numbers, advisory text). This is a correct, useful outcome, not a failure.

3. If a safe fix is possible: create a branch named `devin/bump-{package}-<version>`, bump `{package}`
   to the lowest verified-safe version >= the patched release in {lockfile_hint}. Run the relevant test
   suite. Passing the default test suite is necessary but NOT sufficient if step 1 surfaced a specific
   documented failure mode (e.g. something only observable across Celery task boundaries, or an import
   that only breaks a specific engine spec) -- in that case you must specifically exercise that code
   path, not just rely on the suite being green. Fix any real breakage the bump causes; if a failure is
   clearly unrelated to this bump, note it explicitly rather than silencing it.

4. Run pre-commit / linters on changed files.

5. If and only if you verified a safe fix: open a pull request against `master` titled
   `fix(deps): bump {package} to <version> ({vuln_id})`, describing the CVE, the change, what you
   checked from step 1, and test results. Keep the diff scoped to this dependency bump -- no unrelated
   file changes.

6. Before ending the session, call provide_structured_output with your final verdict per the schema
   provided (outcome, rationale, evidence, prior_constraint_found, pr_url). This is required and is
   what the automation reads to decide how to label the issue -- do not end the session without it.
"""


def handle_issue_labeled(issue: dict) -> None:
    number = issue["number"]
    if any(l["name"] == settings.in_progress_label for l in issue.get("labels", [])):
        log.info("Issue #%s already in progress, ignoring duplicate label event", number)
        return

    # The label check above is only as reliable as the *last* run's ability to
    # write that label back to GitHub. If a prior run created a Devin session
    # (an irreversible, billed call) but then failed before/while setting the
    # label -- a GitHub 401/5xx, a network blip -- GitHub redelivers the
    # webhook, the issue still shows no in-progress label, and without this
    # check we'd spin up a second paid session for the same issue. The DB
    # row is written immediately after session creation succeeds, so this
    # catches that case even when the label update never landed.
    with db.cursor() as cur:
        existing = cur.execute(
            "SELECT 1 FROM sessions WHERE github_issue_number=? AND status NOT IN ('exit', 'error')",
            (number,),
        ).fetchone()
    if existing:
        log.info("Issue #%s already has an active Devin session, ignoring duplicate label event", number)
        return

    prompt = build_prompt(issue)
    resp = devin.create_session(
        prompt=prompt,
        title=f"Superset dep fix: issue #{number}",
        tags=["dependency-remediation", f"issue-{number}"],
        structured_output_schema=REMEDIATION_OUTPUT_SCHEMA,
    )
    session_id = resp["session_id"]
    now = datetime.now(timezone.utc).isoformat()

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO sessions (session_id, github_issue_number, devin_url, prompt, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO NOTHING""",
            (session_id, number, resp.get("url"), prompt, resp.get("status", "new"), now, now),
        )
    db.log_event("devin_session_created", {"issue": number, "session_id": session_id})

    github.set_labels(number, [settings.in_progress_label])
    github.add_comment(
        number,
        f"🤖 Devin session started: {resp.get('url')}\n\nI'll post an update here once it opens a pull request "
        f"(or if it gets stuck / fails).",
    )
    log.info("Started Devin session %s for issue #%s", session_id, number)


def poll_sessions() -> None:
    """Called on a timer. Updates every in-flight session's status; on terminal
    state, comments back on the GitHub issue and relabels it."""
    with db.cursor() as cur:
        rows = cur.execute("SELECT * FROM sessions WHERE status NOT IN ('exit', 'error')").fetchall()

    for row in rows:
        try:
            _poll_one(dict(row))
        except Exception:
            log.exception("Failed polling session %s", row["session_id"])


def _poll_one(row: dict) -> None:
    import json

    from app.devin_client import FAILURE_STATUSES, TERMINAL_STATUSES

    session = devin.get_session(row["session_id"])
    status = session.get("status")
    pull_requests = session.get("pull_requests") or []
    pr_url = pull_requests[0]["pr_url"] if pull_requests else None
    structured = session.get("structured_output") or None
    outcome = structured.get("outcome") if structured else None
    now = datetime.now(timezone.utc).isoformat()
    number = row["github_issue_number"]
    is_new_pr = pr_url and not row["pr_url"]
    prior_outcome = row.get("outcome")
    is_new_outcome = outcome and outcome != prior_outcome

    # Refresh files-changed on every poll while a PR is open and the session
    # hasn't reached a terminal state — Devin can keep pushing commits (e.g.
    # fixing a test failure) to the same PR long after it first appears, so
    # this can't be a one-shot check gated on the PR only just having appeared.
    files_changed = row["files_changed"]
    lockfile_only = row["lockfile_only"]
    if pr_url:
        try:
            pr_number = github.get_pr_number_from_url(pr_url)
            files = github.get_pr_files(pr_number)
            files_changed = len(files)
            lockfile_only = int(is_lockfile_only(files))
        except Exception:
            log.exception("Failed to fetch PR files for %s", pr_url)

    if (
        status == row["status"]
        and pr_url == row["pr_url"]
        and files_changed == row["files_changed"]
        and outcome == prior_outcome
    ):
        return  # truly nothing changed, skip GitHub calls

    finished_at = now if status in TERMINAL_STATUSES else None

    with db.cursor() as cur:
        cur.execute(
            """UPDATE sessions SET status=?, pr_url=?, updated_at=?, finished_at=COALESCE(finished_at, ?),
               files_changed=?, lockfile_only=?, outcome=?, structured_output=? WHERE session_id=?""",
            (
                status,
                pr_url,
                now,
                finished_at,
                files_changed,
                lockfile_only,
                outcome,
                json.dumps(structured) if structured else None,
                row["session_id"],
            ),
        )
    db.log_event(
        "devin_session_updated",
        {"session_id": row["session_id"], "status": status, "pr_url": pr_url, "files_changed": files_changed, "outcome": outcome},
    )

    # A verdict of "blocked" is the priority signal, independent of whether
    # the session status has reached a terminal state yet -- Devin calls
    # provide_structured_output as its last action, so by the time we see it
    # the investigation is done even if polling catches `status` a beat late.
    if outcome == "blocked" and is_new_outcome:
        github.set_labels(number, [settings.blocked_label])
        rationale = structured.get("rationale", "").strip()
        evidence = structured.get("evidence") or []
        evidence_block = ""
        if evidence:
            evidence_lines = "\n".join(f"- {e}" for e in evidence)
            evidence_block = f"**Evidence:**\n{evidence_lines}\n\n"
        github.add_comment(
            number,
            f"🛑 Devin investigated and determined this is **not safe to auto-remediate right now**.\n\n"
            f"{rationale}\n\n"
            f"{evidence_block}"
            f"This needs a human decision (accept the risk, find a different mitigation, or revisit once "
            f"upstream changes). Session: {row['devin_url']}",
        )
        db.log_event("devin_session_blocked", {"session_id": row["session_id"], "issue": number, "rationale": rationale})
    elif is_new_pr and outcome in (None, "fixed"):
        github.set_labels(number, [settings.done_label])
        extra = (
            f" It touched {files_changed} file(s) beyond the lockfile."
            if files_changed and not lockfile_only
            else ""
        )
        github.add_comment(number, f"✅ Devin opened a pull request: {pr_url}.{extra}")
    elif status in FAILURE_STATUSES and not pr_url:
        github.set_labels(number, [settings.failed_label])
        github.add_comment(
            number,
            f"⚠️ The Devin session ended (`{status}`) without producing a pull request. "
            "This needs a human look — remove the label and re-apply it to retry, or fix manually.",
        )
        with db.cursor() as cur:
            cur.execute("UPDATE sessions SET error=? WHERE session_id=?", (f"{status}_without_pr", row["session_id"]))
        db.log_event("devin_session_failed", {"session_id": row["session_id"], "issue": number, "status": status})
    elif status == "exit" and not pr_url and outcome is None:
        # Session ended cleanly, no PR, and no structured output was captured
        # (e.g. an older session created before this schema existed). Fall
        # back to pointing at the raw log instead of silently doing nothing.
        github.add_comment(
            number,
            "ℹ️ The Devin session finished without opening a pull request and without a structured verdict. "
            f"Check the session log for details: {row['devin_url']}",
        )


def file_issue_for_finding(finding) -> dict | None:
    """Idempotently file a GitHub issue for a scanner Finding. Returns None if
    an issue for this exact finding (by content hash) already exists."""
    with db.cursor() as cur:
        existing = cur.execute("SELECT 1 FROM findings WHERE content_hash=?", (finding.content_hash,)).fetchone()
    if existing:
        return None

    severity_label = f"severity/{finding.severity}".lower() if finding.severity != "UNKNOWN" else "severity/unknown"
    body = f"""A known vulnerability was found in a pinned dependency via [OSV.dev](https://osv.dev/vulnerability/{finding.vuln_id}).

**Ecosystem:** {finding.ecosystem}
**Package:** {finding.package}
**Current version:** {finding.current_version}
**Fixed version:** {finding.fixed_version or "see advisory"}
**Vuln id:** {finding.vuln_id}
**Severity:** {finding.severity}

{finding.summary}

---
_Filed automatically by the dependency-remediation scanner. Add the `{settings.trigger_label}` label to have Devin remediate this automatically._
"""
    issue = github.create_issue(
        title=f"[dep] {finding.package} {finding.current_version} has {finding.vuln_id}",
        body=body,
        labels=[severity_label],
    )
    now = datetime.now(timezone.utc).isoformat()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO findings (id, ecosystem, package, current_version, fixed_version, vuln_id, severity,
               summary, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                finding.id,
                finding.ecosystem,
                finding.package,
                finding.current_version,
                finding.fixed_version,
                finding.vuln_id,
                finding.severity,
                finding.summary,
                finding.content_hash,
                now,
            ),
        )
        cur.execute(
            "INSERT INTO issues (finding_id, github_issue_number, github_issue_url, created_at) VALUES (?,?,?,?)",
            (finding.id, issue["number"], issue["html_url"], now),
        )
    db.log_event("issue_filed", {"finding_id": finding.id, "issue": issue["number"]})
    return issue
