"""Dependency vulnerability scanner.

Parses pinned dependencies out of a Python `requirements/*.txt` file and/or a
`package.json`, then batches them against the OSV.dev public vulnerability
database (https://osv.dev — no API key required) to produce real, sourced
findings. Nothing here is fabricated: every finding traces back to an OSV
advisory ID that can be independently verified.
"""

import hashlib
import re
from dataclasses import dataclass, field

import httpx

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{id}"

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-\[\]]+)==([A-Za-z0-9_.\-]+)")


@dataclass
class Finding:
    ecosystem: str
    package: str
    current_version: str
    vuln_id: str
    summary: str
    severity: str
    fixed_version: str | None = None
    aliases: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.ecosystem}:{self.package}:{self.vuln_id}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.id.encode()).hexdigest()[:16]


def parse_requirements_txt(text: str) -> dict[str, str]:
    """Extract exact pins (name==version) from a pip-compile style lock file."""
    deps: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split(" ;")[0].strip()  # drop environment markers
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = _PIN_RE.match(line)
        if m:
            name = m.group(1).split("[")[0].lower()
            deps[name] = m.group(2)
    return deps


def parse_package_json(data: dict) -> dict[str, str]:
    """Extract exact-ish versions from package.json dependencies/devDependencies.

    Only entries without range operators (or with a leading `=`) are treated as
    pinned; ranged entries (^, ~, >=) are skipped because OSV needs a concrete
    version to evaluate against.
    """
    deps: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        for name, version in data.get(section, {}).items():
            v = version.lstrip("=")
            if re.match(r"^\d+\.\d+\.\d+", v):
                deps[name] = v
    return deps


def _osv_batch_query(deps: dict[str, str], ecosystem: str) -> dict[str, list[str]]:
    """Returns {package_name: [vuln_id, ...]} for packages with known advisories."""
    names = list(deps.keys())
    if not names:
        return {}
    queries = [{"package": {"name": n, "ecosystem": ecosystem}, "version": v} for n, v in deps.items()]
    results: dict[str, list[str]] = {}
    with httpx.Client(timeout=30) as client:
        # OSV caps batch size; chunk defensively.
        for i in range(0, len(queries), 100):
            chunk_names = names[i : i + 100]
            chunk_queries = queries[i : i + 100]
            resp = client.post(OSV_BATCH_URL, json={"queries": chunk_queries})
            resp.raise_for_status()
            for name, entry in zip(chunk_names, resp.json().get("results", [])):
                vulns = entry.get("vulns") or []
                if vulns:
                    results[name] = [v["id"] for v in vulns]
    return results


def _osv_vuln_detail(vuln_id: str, client: httpx.Client) -> dict:
    resp = client.get(OSV_VULN_URL.format(id=vuln_id), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _severity_of(detail: dict) -> str:
    """Prefer the qualitative label (LOW/MODERATE/HIGH/CRITICAL) that GitHub's
    reviewers attach, falling back to the raw CVSS vector when unreviewed."""
    db_specific = detail.get("database_specific", {})
    label = db_specific.get("severity")
    if label:
        return label
    for sev in detail.get("severity", []):
        if sev.get("type") == "CVSS_V3":
            return sev.get("score", "UNKNOWN")
    return "UNKNOWN"


def _fixed_version_of(detail: dict, ecosystem: str, package: str) -> str | None:
    for affected in detail.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("name") != package or pkg.get("ecosystem") != ecosystem:
            continue
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None


def scan(deps: dict[str, str], ecosystem: str) -> list[Finding]:
    """Scan a {package: version} map against OSV.dev and return real findings.

    OSV frequently cross-lists the same underlying vulnerability under multiple
    IDs (e.g. a GHSA advisory and its PYSEC alias). We only want one issue per
    real vulnerability, so once a vuln's alias set has been seen for a package
    we skip the duplicate rather than filing it again.
    """
    hits = _osv_batch_query(deps, ecosystem)
    findings: list[Finding] = []
    with httpx.Client(timeout=30) as client:
        for package, vuln_ids in hits.items():
            current_version = deps[package]
            seen_ids: set[str] = set()
            for vuln_id in vuln_ids:
                if vuln_id in seen_ids:
                    continue
                detail = _osv_vuln_detail(vuln_id, client)
                aliases = detail.get("aliases", [])
                seen_ids.add(vuln_id)
                seen_ids.update(aliases)
                findings.append(
                    Finding(
                        ecosystem=ecosystem,
                        package=package,
                        current_version=current_version,
                        vuln_id=vuln_id,
                        summary=detail.get("summary") or detail.get("details", "")[:200],
                        severity=_severity_of(detail),
                        fixed_version=_fixed_version_of(detail, ecosystem, package),
                        aliases=aliases,
                    )
                )
    return findings
