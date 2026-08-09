"""Aggregates the sqlite audit trail into the numbers an eng leader actually wants:
throughput, success rate, and what's stuck right now."""

import json

from app import db


def compute() -> dict:
    with db.cursor() as cur:
        findings_count = cur.execute("SELECT COUNT(*) c FROM findings").fetchone()["c"]
        issues_count = cur.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"]
        sessions = [dict(r) for r in cur.execute("SELECT * FROM sessions").fetchall()]
        recent_events = [
            dict(r) for r in cur.execute("SELECT * FROM events ORDER BY id DESC LIMIT 25").fetchall()
        ]

    # A session counts as resolved once it has a verdict, not only once Devin's
    # own `status` field flips to a terminal value: provide_structured_output
    # is the agent's last action before it wraps up, so `outcome` (what
    # orchestrator._poll_one already acted on -- labeling, commenting) can
    # land a poll cycle or two before `status` catches up. Without this, the
    # dashboard would show a session as "active" for a stretch after GitHub
    # already shows it resolved -- a visible inconsistency, not just a
    # cosmetic lag.
    def _is_resolved(s: dict) -> bool:
        return s["status"] in ("exit", "error") or s["outcome"] is not None

    active = [s for s in sessions if not _is_resolved(s)]
    terminal = [s for s in sessions if _is_resolved(s)]
    succeeded = [s for s in terminal if s["pr_url"]]
    blocked = [s for s in terminal if s["outcome"] == "blocked"]
    # "Failed" now means Devin's session actually errored out with nothing to
    # show -- a technical failure. A session that investigated and correctly
    # declined to open a PR (outcome == "blocked") is not a failure; it's
    # counted separately so it doesn't drag down the number that's supposed
    # to answer "is this actually working."
    failed = [s for s in terminal if not s["pr_url"] and s["outcome"] != "blocked"]

    # "Resolved" = Devin reached a real, justified verdict (fixed or blocked)
    # rather than erroring out. This is the number that answers "is the
    # judgment loop working," as distinct from "did every issue get a PR."
    resolved = succeeded + blocked
    resolution_rate = (len(resolved) / len(terminal) * 100) if terminal else None
    success_rate = (len(succeeded) / len(terminal) * 100) if terminal else None

    # Throughput: PRs opened per day, last 14 days, for the dashboard sparkline.
    by_day: dict[str, int] = {}
    for s in succeeded:
        day = s["finished_at"][:10]
        by_day[day] = by_day.get(day, 0) + 1
    throughput = sorted(by_day.items())[-14:]

    blocked_sessions = []
    for s in blocked:
        rationale = ""
        if s.get("structured_output"):
            try:
                rationale = json.loads(s["structured_output"]).get("rationale", "")
            except (json.JSONDecodeError, TypeError):
                pass
        blocked_sessions.append({**s, "rationale": rationale})

    return {
        "findings_count": findings_count,
        "issues_filed": issues_count,
        "sessions_total": len(sessions),
        "sessions_active": len(active),
        "sessions_succeeded": len(succeeded),
        "sessions_blocked": len(blocked),
        "sessions_failed": len(failed),
        "success_rate_pct": round(success_rate, 1) if success_rate is not None else None,
        "resolution_rate_pct": round(resolution_rate, 1) if resolution_rate is not None else None,
        "throughput_by_day": throughput,
        "active_sessions": active,
        "blocked_sessions": blocked_sessions,
        "recent_events": recent_events,
    }
