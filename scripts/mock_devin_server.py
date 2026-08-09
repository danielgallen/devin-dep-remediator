#!/usr/bin/env python3
"""A tiny stand-in for the Devin v3 Organization API, for demoing or testing
the orchestrator end-to-end without spending real ACUs or requiring network
access to api.devin.ai. Point DEVIN_API_BASE at this server's URL and
DEVIN_API_KEY at any non-empty string to use it.

Sessions transition new -> running -> exit (with a fake pull_request) after
SESSION_DURATION_SECONDS, so the poller and dashboard have something real to
show without waiting on an actual coding agent.

Usage:
    uvicorn scripts.mock_devin_server:app --port 8081
"""

import itertools
import time

from fastapi import FastAPI

app = FastAPI(title="mock-devin")

SESSION_DURATION_SECONDS = 45
_sessions: dict[str, dict] = {}
_counter = itertools.count(1)

# Keyed by the "issue-N" tag the orchestrator always includes. Lets demo mode
# rehearse the real verdicts (fixed vs. blocked) without spending ACUs or
# waiting on a live session -- issue-2 (flask) and issue-3 (paramiko) mirror
# the two real traps this system is built to catch; anything else "fixes."
_SCRIPTED_OUTCOMES: dict[str, dict] = {
    "issue-2": {
        "outcome": "blocked",
        "rationale": (
            "Fixing this requires bumping flask-sqlalchemy to 3.0.5 (Flask 3 removes "
            "flask._app_ctx_stack, which 2.5.1 imports). pyproject.toml already pins "
            "flask-sqlalchemy below 3.0 with a comment citing PR #42542: that exact bump was "
            "attempted before and reverted after real CI runs hit 'NoneType' attribute errors "
            "and MySQL lock-wait timeouts caused by FSA 3.0 scoping sessions by Flask "
            "app-context instead of thread identity, which breaks session handling across "
            "Celery task boundaries. The default test suite does not exercise that boundary, "
            "so a green run here would not actually rule the regression out."
        ),
        "evidence": [
            "pyproject.toml: \"Pinned explicitly below 3.0 ... structural incompatibility with Superset's current session/app-context handling across Celery task boundaries (see PR #42542)\"",
            "apache/superset#42542: reverted after 'NoneType' object has no attribute errors + MySQL Lock wait timeout failures",
        ],
        "prior_constraint_found": True,
    },
    "issue-3": {
        "outcome": "blocked",
        "rationale": (
            "GHSA-r374-rxx8-8654 has no patched release listed by OSV/GHSA as of this session -- "
            "there is no version to bump to that resolves the advisory. Separately, even a "
            "hypothetical paramiko 4.0 bump is already blocked in this repo: pyproject.toml pins "
            "paramiko<4.0 because 4.0 removed DSSKey, which sshtunnel still imports at module "
            "scope, so bumping would break SSH tunnel database connections outright."
        ),
        "evidence": [
            "GHSA-r374-rxx8-8654: no 'fixed' event in OSV data as of scan time",
            "pyproject.toml: \"paramiko>=3.4.0, <4.0  # 4.0 removed DSSKey, still referenced by sshtunnel\"",
        ],
        "prior_constraint_found": True,
    },
}


@app.get("/self")
def self_():
    return {
        "principal_type": "service_user",
        "service_user_id": "su_mock",
        "service_user_name": "mock-service-user",
        "org_id": "org_mock",
    }


@app.post("/organizations/{org_id}/sessions")
def create_session(org_id: str, body: dict):
    n = next(_counter)
    session_id = f"ses-mock-{n:04d}"
    now = time.time()
    _sessions[session_id] = {
        "session_id": session_id,
        "org_id": org_id,
        "url": f"https://devin.ai/sessions/{session_id}",
        "status": "running",
        "title": body.get("title"),
        "tags": body.get("tags", []),
        "created_at": int(now),
        "updated_at": int(now),
        "_started": now,
        "pull_requests": [],
    }
    return {k: v for k, v in _sessions[session_id].items() if not k.startswith("_")}


@app.get("/organizations/{org_id}/sessions/{session_id}")
def get_session(org_id: str, session_id: str):
    s = _sessions[session_id]
    elapsed = time.time() - s["_started"]
    if elapsed > SESSION_DURATION_SECONDS and s["status"] != "exit":
        s["status"] = "exit"
        issue_tag = next((t for t in s["tags"] if t.startswith("issue-")), "issue-0")
        scripted = _SCRIPTED_OUTCOMES.get(issue_tag)
        if scripted:
            s["pull_requests"] = []
            s["structured_output"] = {**scripted, "pr_url": None}
        else:
            pr_url = f"https://github.com/mock/superset/pull/{issue_tag.split('-')[-1]}"
            s["pull_requests"] = [{"pr_url": pr_url, "pr_state": "open"}]
            s["structured_output"] = {
                "outcome": "fixed",
                "rationale": "Version bump resolved cleanly; test suite passed with no related breakage.",
                "evidence": [],
                "prior_constraint_found": False,
                "pr_url": pr_url,
            }
        s["updated_at"] = int(time.time())
    return {k: v for k, v in s.items() if not k.startswith("_")}
