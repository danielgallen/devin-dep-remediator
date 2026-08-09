"""GitHub webhook ingress + dashboard/metrics routes."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app import db
from app.config import settings
from app.orchestrator import handle_issue_labeled

log = logging.getLogger("webhook")
router = APIRouter()


def _verify_signature(payload: bytes, signature_header: str | None) -> None:
    if not settings.github_webhook_secret:
        raise HTTPException(500, "GITHUB_WEBHOOK_SECRET is not configured")
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(401, "Missing or malformed X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(401, "Signature mismatch")


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    raw = await request.body()
    _verify_signature(raw, x_hub_signature_256)
    payload = await request.json()

    db.log_event("webhook_received", {"event": x_github_event, "delivery": x_github_delivery})

    if x_github_event == "issues" and payload.get("action") == "labeled":
        label = (payload.get("label") or {}).get("name")
        if label == settings.trigger_label:
            issue = payload["issue"]
            log.info("Trigger label '%s' applied to issue #%s — starting Devin session", label, issue["number"])
            handle_issue_labeled(issue)
            return {"status": "session_started", "issue": issue["number"]}

    return {"status": "ignored"}


@router.get("/healthz")
def healthz():
    return {"status": "ok"}
