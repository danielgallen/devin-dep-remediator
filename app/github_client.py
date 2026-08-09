"""Thin wrapper around the GitHub REST API for the target fork."""

import httpx

from app.config import settings


class GitHubClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        self.repo = settings.github_repo  # "owner/name"

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        resp = self._client.post(f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels})
        resp.raise_for_status()
        return resp.json()

    def get_issue(self, number: int) -> dict:
        resp = self._client.get(f"/repos/{self.repo}/issues/{number}")
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, number: int, body: str) -> dict:
        resp = self._client.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})
        resp.raise_for_status()
        return resp.json()

    def set_labels(self, number: int, labels: list[str]) -> dict:
        resp = self._client.put(f"/repos/{self.repo}/issues/{number}/labels", json={"labels": labels})
        resp.raise_for_status()
        return resp.json()

    def remove_label(self, number: int, label: str) -> None:
        resp = self._client.delete(f"/repos/{self.repo}/issues/{number}/labels/{label}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    def ensure_labels_exist(self, labels_with_colors: dict[str, str]) -> None:
        for name, color in labels_with_colors.items():
            resp = self._client.post(f"/repos/{self.repo}/labels", json={"name": name, "color": color})
            if resp.status_code not in (201, 422):  # 422 = already exists
                resp.raise_for_status()

    def get_pr_files(self, pr_number: int) -> list[str]:
        """Paths changed in a PR — the raw signal for 'did this touch more than
        the lockfile', which is what distinguishes a verified fix from a
        version-string edit."""
        files: list[str] = []
        page = 1
        while True:
            resp = self._client.get(f"/repos/{self.repo}/pulls/{pr_number}/files", params={"per_page": 100, "page": page})
            resp.raise_for_status()
            batch = resp.json()
            files.extend(f["filename"] for f in batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_pr_number_from_url(self, pr_url: str) -> int:
        return int(pr_url.rstrip("/").rsplit("/", 1)[-1])
