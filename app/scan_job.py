"""Periodic scan: re-check Superset's dependency manifests against OSV.dev and
file GitHub issues for any newly discovered vulnerabilities. Safe to run on a
schedule — file_issue_for_finding dedupes on content hash, so re-scanning
never spams duplicate issues for a finding already on file."""

import logging

import httpx

from app import scanner
from app.config import settings
from app.orchestrator import file_issue_for_finding

log = logging.getLogger("scan_job")

RAW_BASE = "https://raw.githubusercontent.com/{repo}/master/{path}"


def _fetch(repo: str, path: str) -> str:
    url = RAW_BASE.format(repo=repo, path=path)
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def run_scan() -> dict:
    repo = settings.github_repo
    if not repo:
        log.warning("GITHUB_REPO not set, skipping scan")
        return {"filed": 0}

    findings = []
    try:
        base_txt = _fetch(repo, "requirements/base.txt")
        py_deps = scanner.parse_requirements_txt(base_txt)
        findings += scanner.scan(py_deps, "PyPI")
    except httpx.HTTPError:
        log.exception("Failed to fetch/parse requirements/base.txt")

    try:
        import json

        pkg_json = json.loads(_fetch(repo, "superset-frontend/package.json"))
        js_deps = scanner.parse_package_json(pkg_json)
        findings += scanner.scan(js_deps, "npm")
    except httpx.HTTPError:
        log.exception("Failed to fetch/parse superset-frontend/package.json")

    filed = 0
    for finding in findings:
        issue = file_issue_for_finding(finding)
        if issue:
            filed += 1
            log.info("Filed issue #%s for %s (%s)", issue["number"], finding.package, finding.vuln_id)

    log.info("Scan complete: %s findings, %s new issues filed", len(findings), filed)
    return {"findings": len(findings), "filed": filed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_scan())
