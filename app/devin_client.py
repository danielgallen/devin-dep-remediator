"""Thin wrapper around the Devin v3 Organization Sessions API.

Reference:
  https://docs.devin.ai/api-reference/v3/self/self
  https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions
  https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session

Auth: a service-user credential (prefix `cog_`), created under
Organization Settings > Service Users in the Devin dashboard. Unlike the
legacy v1 personal/service API keys (`apk_*`), this token is scoped to an
organization and every call is attributable to that service user in Devin's
own audit log — the right shape for an unattended, event-driven system.
"""

import httpx

from app.config import settings

# v3 status values (distinct from the deprecated v1 `status_enum` set).
TERMINAL_STATUSES = {"exit", "error"}
FAILURE_STATUSES = {"error"}


class DevinClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=settings.devin_api_base,
            headers={"Authorization": f"Bearer {settings.devin_api_key}"},
            timeout=30,
        )
        self._org_id: str | None = settings.devin_org_id or None

    @property
    def org_id(self) -> str:
        if not self._org_id:
            self._org_id = self._resolve_org_id()
        return self._org_id

    def _resolve_org_id(self) -> str:
        """Service-user tokens are scoped to a single org; GET /v3/self returns
        it so operators don't have to hunt it down and paste it into .env."""
        resp = self._client.get("/self")
        resp.raise_for_status()
        data = resp.json()
        org_id = data.get("org_id")
        if not org_id:
            raise RuntimeError(
                "Devin service-user token did not resolve to an org_id via GET /v3/self; "
                "set DEVIN_ORG_ID explicitly in .env."
            )
        return org_id

    def create_session(
        self,
        prompt: str,
        title: str,
        tags: list[str],
        max_acu_limit: int | None = None,
        structured_output_schema: dict | None = None,
    ) -> dict:
        body = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "devin_mode": "normal",
            "resumable": True,
        }
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if structured_output_schema:
            # Forces the session to end by calling provide_structured_output
            # against this schema instead of just trailing off in prose --
            # the orchestrator reads session["structured_output"] to decide
            # fixed vs. blocked instead of guessing from "did a PR appear."
            body["structured_output_schema"] = structured_output_schema
            body["structured_output_required"] = True
        resp = self._client.post(f"/organizations/{self.org_id}/sessions", json=body)
        resp.raise_for_status()
        return resp.json()

    def get_session(self, session_id: str) -> dict:
        resp = self._client.get(f"/organizations/{self.org_id}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()
