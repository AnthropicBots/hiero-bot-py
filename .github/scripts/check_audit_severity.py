"""Fail CI if pip-audit found any High/Critical severity vulnerabilities.

pip-audit has no built-in --min-severity flag, and OSV advisory data doesn't
always carry a CVSS score in the JSON report itself. This reads the JSON
report pip-audit produces and falls back to the OSV advisory API for a
finding's severity metadata when the report omits `severity`.

Findings without a resolvable score are printed for visibility but do not
fail CI.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from cvss import CVSS3, CVSS4

HIGH_CRITICAL_THRESHOLD = 7.0
OSV_URL_TEMPLATE = "https://api.osv.dev/v1/vulns/{id}"
SCORED_SEVERITY_TYPES = {"CVSS_V3", "CVSS_V4"}

# Cache OSV lookups by advisory id/alias for the life of the process, since
# the same advisory commonly appears across multiple affected dependencies.
_osv_cache: dict[str, dict[str, Any] | None] = {}


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    vuln_id: str
    severity_label: str
    score: float | None


def _fetch_osv_record(identifier: str) -> dict[str, Any] | None:
    if identifier in _osv_cache:
        return _osv_cache[identifier]

    record: dict[str, Any] | None = None
    try:
        with urlopen(OSV_URL_TEMPLATE.format(id=identifier), timeout=10) as response:
            record = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, TimeoutError, ValueError):
        record = None

    _osv_cache[identifier] = record
    return record


def _parse_cvss_number(value: Any) -> float | None:
    """Return the numeric CVSS base score for a severity payload's `score`.

    The `score` field from pip-audit/OSV for CVSS_V3/CVSS_V4 entries is a
    full vector string (e.g. "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"),
    not a bare number. The leading "CVSS:3.1" segment is the spec version,
    not the score, so the base score must be computed from the vector
    rather than extracted from the string with a regex.
    """
    if isinstance(value, (int, float)):
        return float(value)

    if not isinstance(value, str):
        return None

    # Some tools/report shapes set a plain numeric string directly.
    try:
        return float(value)
    except ValueError:
        pass

    try:
        if value.startswith("CVSS:4"):
            return CVSS4(value).base_score
        if value.startswith("CVSS:3") or value.startswith("AV:"):
            # CVSS3() accepts either a bare metric string or one prefixed
            # with "CVSS:3.x/".
            return CVSS3(value).base_score
    except Exception:
        return None

    return None


def _score_from_severity_entries(entries: list[Any]) -> float | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type", "").upper() in SCORED_SEVERITY_TYPES:
            score = _parse_cvss_number(entry.get("score"))
            if score is not None:
                return score
    return None


def _severity_label(severity: Any) -> str:
    if isinstance(severity, str):
        return severity.upper()

    if isinstance(severity, dict):
        return severity.get("type", "UNKNOWN").upper()

    if isinstance(severity, list):
        for entry in severity:
            if isinstance(entry, dict) and entry.get("type"):
                return entry["type"].upper()

    return "UNKNOWN"


def get_cvss_score(vuln: dict[str, Any]) -> float | None:
    severity = vuln.get("severity")

    if isinstance(severity, str):
        return HIGH_CRITICAL_THRESHOLD if severity.upper() in {"HIGH", "CRITICAL"} else None

    if isinstance(severity, dict):
        if severity.get("type", "").upper() in SCORED_SEVERITY_TYPES:
            return _parse_cvss_number(severity.get("score"))
        return None

    if isinstance(severity, list):
        score = _score_from_severity_entries(severity)
        if score is not None:
            return score

    # No usable severity on the report entry itself -- fall back to OSV,
    # trying the primary id first and then each alias.
    identifiers = [vuln.get("id"), *vuln.get("aliases", [])]
    for identifier in filter(None, identifiers):
        record = _fetch_osv_record(identifier)
        if not record:
            continue

        osv_severity = record.get("severity")
        if isinstance(osv_severity, list):
            score = _score_from_severity_entries(osv_severity)
            if score is not None:
                return score

    return None


def collect_findings(report: dict[str, Any]) -> list[Finding]:
    findings = []
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            score = get_cvss_score(vuln)
            findings.append(
                Finding(
                    package=dep["name"],
                    version=dep["version"],
                    vuln_id=vuln["id"],
                    severity_label=_severity_label(vuln.get("severity")),
                    score=score,
                )
            )
    return findings


def main(path: str) -> None:
    with open(path) as f:
        report = json.load(f)

    findings = collect_findings(report)
    unscored = [f for f in findings if f.score is None]
    flagged = [f for f in findings if f.score is not None and f.score >= HIGH_CRITICAL_THRESHOLD]

    if unscored:
        print("Findings with no CVSS score (not blocking, review manually):")
        for f in unscored:
            print(f"  {f.package}=={f.version}  {f.vuln_id}")

    if flagged:
        print("\nHigh/Critical vulnerabilities found:")
        for f in flagged:
            print(f"  {f.package}=={f.version}  {f.vuln_id}  {f.severity_label}  CVSS {f.score}")
        sys.exit(1)

    print("\nNo high or critical vulnerabilities found.")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1])