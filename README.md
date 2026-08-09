# devin-dep-remediator

Event-driven dependency-vulnerability remediation for
[apache/superset](https://github.com/apache/superset), built on the
[Devin](https://devin.ai) API.

A scheduled scan checks Superset's pinned dependencies against
[OSV.dev](https://osv.dev) and files a GitHub issue for each real finding
(see [`sample_issues/`](sample_issues/)). When a human applies the
`devin-fix` label, a webhook starts a Devin session that investigates and,
if and only if it can verify a safe fix, opens a pull request. Every state
transition is logged to a dashboard.

## Architecture

```
                    ┌─────────────────────────┐
   scheduled  ─────▶│   scanner.py             │  parses requirements/base.txt +
   (APScheduler)    │   (OSV.dev batch query)  │  package.json, queries OSV.dev
                    └────────────┬─────────────┘
                                 │ real, sourced findings
                                 ▼
                    ┌─────────────────────────┐
                    │  orchestrator.py          │──▶ GitHub Issues API
                    │  file_issue_for_finding   │    (dedup on content hash)
                    └─────────────────────────┘
                                 │
                                 │ human applies `devin-fix` label
                                 ▼
   GitHub  ───webhook───▶ ┌─────────────────────────┐
   (issues.labeled)       │  webhook.py              │  HMAC-verified
                          │  -> handle_issue_labeled │
                          └────────────┬─────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │  devin_client.py         │  Devin v3 Organization API
                          │  POST /organizations/    │  (service-user token) with
                          │  {org_id}/sessions        │  structured_output_schema +
                          └────────────┬─────────────┘  structured_output_required=True
                                       │ session_id
                                       ▼
                          ┌─────────────────────────┐
        every 30s ───────▶│  orchestrator.poll_sessions │  GET session, read
        (APScheduler)     │                          │  structured_output.outcome
                          └────────────┬─────────────┘  ("fixed" | "blocked"),
                                       │                comment + relabel
                     ┌─────────────────┴──────────────────┐
                     ▼                                     ▼
             outcome == "fixed"                   outcome == "blocked"
             label: devin-pr-open                 label: devin-blocked
             (PR opened, verified)                (rationale + evidence
                     │                             posted to the issue)
                     └─────────────────┬──────────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │  sqlite (db.py)          │  every state transition is
                          │  findings/issues/         │  an audit-log row
                          │  sessions/events          │
                          └────────────┬─────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │  /dashboard, /api/metrics│  success rate, MTTR,
                          │                          │  throughput, active sessions
                          └─────────────────────────┘
```

One FastAPI service (`app/main.py`) with two background jobs (APScheduler)
and a webhook route — no queue, no separate worker; the volume here (one
repo's dependency findings) doesn't need one.

## Design notes

**`"blocked"` is a first-class outcome, not a failure.** Not every CVE has
a safe fix at scan time — a patched release might not exist yet, or the fix
might reintroduce a regression the codebase already hit and reverted. Each
Devin v3 session is created with a `structured_output_schema`
(`orchestrator.REMEDIATION_OUTPUT_SCHEMA`) and `structured_output_required=True`,
so a session can't end without an explicit `"fixed"` or `"blocked"` verdict
plus a written rationale and cited evidence, instead of the automation
inferring success from whether a PR happened to appear. `"blocked"` gets its
own label (`devin-blocked`) and dashboard section, tracked separately from
an actual session error (`devin-failed`). `orchestrator.build_prompt`
instructs Devin to check for existing version constraints and their history
(comments, linked issues/PRs) before proposing a change — a passing test
suite isn't sufficient evidence a bump is safe if it re-crosses a boundary
the codebase has already reverted once.

**Naive-bump comparison.** For two of the filed findings, a hand-authored
"naive bump" PR (version string only, no tests — what Dependabot or a bump
script produces) is registered alongside Devin's PR
(`orchestrator.record_naive_pr` / `scripts/register_naive_pr.py`), so the
dashboard can show files-changed-beyond-the-lockfile side by side. If
Devin's PR is also lockfile-only, that's an honest result: the bump was
safe and a script would have sufficed.

## Repo layout

```
app/
  scanner.py        OSV.dev-backed vulnerability scanning
  scan_job.py        periodic job: scan -> file_issue_for_finding
  orchestrator.py    prompt building, Devin session lifecycle,
                     GitHub issue/label/comment state machine
  devin_client.py    Devin v3 Organization API client
  github_client.py   GitHub REST client
  webhook.py         signed webhook ingress
  metrics.py         aggregates the audit log into dashboard numbers
  db.py              sqlite schema + audit log
  main.py            FastAPI app, background jobs, dashboard route
  templates/dashboard.html
scripts/
  seed_fork_issues.py     one-shot: run the scan job now
  register_webhook.py     wires the GitHub webhook to a public URL via `gh`
  simulate_webhook.py     fires a signed webhook locally against a real issue
  mock_devin_server.py    stand-in Devin API for offline/no-ACU runs
  register_naive_pr.py    registers a naive-bump control PR for comparison
sample_issues/             filed issue bodies (real CVE data)
```

## Running it

### 1. Configure

```bash
cp .env.example .env
```

Fill in:
- `DEVIN_API_KEY` — a **service-user token** (`cog_...`) from Devin:
  Organization Settings → Service Users. Uses the v3 Organization API, so
  every session is attributable to that service user in Devin's own audit
  log.
- `GITHUB_TOKEN` — a token (fine-grained PAT recommended, scoped to just the
  fork) with issues + contents access on your fork.
- `GITHUB_REPO` — `youruser/superset`, your fork.
- `GITHUB_WEBHOOK_SECRET` — `openssl rand -hex 32`.

### 2. Fork + seed

```bash
gh repo fork apache/superset --clone=false
gh api repos/<you>/superset -X PATCH -f has_issues=true   # forks have Issues off by default
python scripts/seed_fork_issues.py                        # real scan, real issues filed
```

### 3. Run

```bash
docker compose up --build
# or locally:
pip install -r requirements.txt && uvicorn app.main:app --port 8080
```

Dashboard: `http://localhost:8080/dashboard`

### 4. Wire the trigger

For a real webhook (needs a public URL, e.g. via a tunnel):
```bash
python scripts/register_webhook.py https://your-public-url.example.com
```

To run locally without exposing anything publicly:
```bash
gh issue edit <n> -R <you>/superset --add-label devin-fix
python scripts/simulate_webhook.py <n>
```

Both paths hit the exact same `handle_issue_labeled` code — the simulator
just replaces the network hop from GitHub to your machine with a direct
call, signed with the same secret. It still pulls the real issue body from
GitHub.

### Demo mode (no ACUs, no real Devin calls)

```bash
docker compose --profile demo up --build
# set in .env: DEVIN_API_BASE=http://mock-devin:8081  DEVIN_API_KEY=mock
```

`scripts/mock_devin_server.py` implements the same three v3 endpoints
(`/self`, `POST sessions`, `GET sessions/{id}`) and transitions a session to
`exit` after 45s with a scripted `structured_output`, so the
poller/dashboard/labeling loop — including the `blocked` path — is fully
exercised without touching the real API.

## Observability

`/dashboard` (and `/api/metrics` as raw JSON) shows:
- Findings discovered / issues filed / sessions total
- Active sessions, with links to both the GitHub issue and the Devin session
- **PRs opened** vs. **blocked (needs human)** vs. **failed (errored)**
- **Resolution rate** — (PRs opened + correctly blocked) / terminal sessions
- **Blocked findings** — Devin's own rationale, shown next to the issue
- **Devin vs. naive** comparison table
- Mean time to PR, throughput (PRs/day), and a raw event/audit log

All of it is derived from the same `events` table the webhook and poller
write to as they run.

## Possible extensions

- **Severity-gated auto-trigger**: skip the human-applies-a-label step for
  `CRITICAL` findings above a policy threshold, keep the gate for everything
  else.
- **CI as the confidence gate**: poll the PR's check-run status before
  marking an issue `devin-pr-open` vs. a `devin-needs-review`.
- **Fan out beyond one repo**: `GITHUB_REPO` becomes a list; a queue
  (SQS/Redis) replaces the in-process APScheduler at that point.
- **Other trigger sources**: `handle_issue_labeled` works the same from a
  Slack slash command, a Jira webhook, or a nightly SAST scan instead of OSV.
- **Cost tracking**: the v3 API returns `acus_consumed` per session; wiring
  that into `metrics.py` turns "mean time to PR" into "cost per fix."
- **Policy for blocked findings**: e.g. auto-recheck on a schedule instead
  of only re-triggering on label, for cases blocked on an upstream patch
  that hasn't shipped yet.
