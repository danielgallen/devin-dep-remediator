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
             (PR opened; Devin's own               (rationale + evidence
              claim, spot-checked via                posted to the issue)
              lockfile-only file diff)
                     └─────────────────┬──────────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │  sqlite (db.py)          │  every state transition is
                          │  findings/issues/         │  an audit-log row
                          │  sessions/events          │
                          └────────────┬─────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │  /dashboard, /api/metrics│  success rate,
                          │                          │  throughput, active sessions
                          └─────────────────────────┘
```

One FastAPI service (`app/main.py`) with two background jobs (APScheduler)
and a webhook route — no queue, no separate worker; the volume here (one
repo's dependency findings) doesn't need one.

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
sample_issues/             filed issue bodies (real CVE data)
```

## Running it

The app and its scripts run in Docker — you don't need a local Python
install for most of this. You do need Docker, Docker Compose, and an
authenticated [`gh` CLI](https://cli.github.com/) (`gh auth login`) for the
fork step and for `register_webhook.py`, which shells out to `gh` directly
(see step 4).

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
  fork) with **Issues: read/write** and **Pull requests: read** access on
  your fork (`github_client.get_pr_files` reads changed-file lists off
  opened PRs; nothing here touches repo contents directly).
- `GITHUB_REPO` — `youruser/superset`, your fork.
- `GITHUB_WEBHOOK_SECRET` — `openssl rand -hex 32`.

`docker compose` reads `.env` directly (`env_file:` in
[`docker-compose.yml`](docker-compose.yml)), so this file must exist —
even with placeholder values — before step 3, or `docker compose up` will
fail to start.

### 2. Fork Superset

```bash
gh repo fork apache/superset --clone=false
gh api repos/<you>/superset -X PATCH -f has_issues=true   # forks have Issues off by default
```

### 3. Build and start

```bash
docker compose up --build -d
```

Dashboard: [http://localhost:8080/dashboard](http://localhost:8080/dashboard)
Health check: `GET /healthz`

This starts the FastAPI app with both background jobs running (session
poller + the periodic OSV scan, every `SCAN_INTERVAL_SECONDS`, 24h by
default). To see findings immediately instead of waiting for the first
scheduled scan, run the scan once, synchronously, inside the running image:

```bash
docker compose run --rm app python scripts/seed_fork_issues.py
```

This files real GitHub issues on your fork (see [`sample_issues/`](sample_issues/)
for what one looks like).

### 4. Wire the trigger

For a real webhook (needs a public URL, e.g. via a tunnel such as `ngrok` or
`cloudflared`), run this one **on the host**, not via `docker compose run` —
it shells out to `gh`, which isn't installed in the container:
```bash
python scripts/register_webhook.py https://your-public-url.example.com
```
(needs `pip install -r requirements.txt` on the host first, since it imports
`app.config`.)

To trigger locally without exposing anything publicly:
```bash
gh issue edit <n> -R <you>/superset --add-label devin-fix
docker compose run --rm app python scripts/simulate_webhook.py <n> --url http://app:8080
```

Both paths hit the exact same `handle_issue_labeled` code — the simulator
just replaces the network hop from GitHub to your machine with a direct
call, signed with the same secret. It still pulls the real issue body from
GitHub. (`--url http://app:8080` targets the running `app` service by its
Docker Compose network name; omit it if you're running the app locally with
`uvicorn` instead, where the default `http://localhost:8080` is correct.)

### Running locally without Docker

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

Everything above (`seed_fork_issues.py`, `register_webhook.py`,
`simulate_webhook.py`) can then be run directly with `python scripts/...`
instead of via `docker compose run`.

### Demo mode (no ACUs, no real Devin calls)

```bash
docker compose --profile demo up --build -d
# set in .env: DEVIN_API_BASE=http://mock-devin:8081  DEVIN_API_KEY=mock
```

`DEVIN_API_BASE=http://mock-devin:8081` only resolves inside the Compose
network (it's the `mock-devin` service's container name), so demo mode
requires running through `docker compose --profile demo up`, not the local
`uvicorn` path above.

`scripts/mock_devin_server.py` implements the same three v3 endpoints
(`/self`, `POST sessions`, `GET sessions/{id}`) and transitions a session to
`exit` after 45s with a scripted `structured_output`, so the
poller/dashboard/labeling loop — including the `blocked` path — is fully
exercised without touching the real API.

### Stopping / data

```bash
docker compose down          # stop, keep the sqlite volume (remediator_data)
docker compose down -v       # stop and wipe all findings/issues/sessions data
```

## Observability

`/dashboard` (and `/api/metrics` as raw JSON) shows:
- Findings discovered / issues filed / sessions total
- Active sessions, with links to both the GitHub issue and the Devin session
- **PRs opened** vs. **blocked (needs human)** vs. **failed (errored)**
- **Resolution rate** — (PRs opened + correctly blocked) / terminal sessions
- **Blocked findings** — Devin's own rationale, shown next to the issue
- Throughput (PRs/day) and a raw event/audit log

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
  that into `metrics.py` gives a "cost per fix" number.
- **Policy for blocked findings**: e.g. auto-recheck on a schedule instead
  of only re-triggering on label, for cases blocked on an upstream patch
  that hasn't shipped yet.
