"""Fail CI if pip-audit found any High/Critical severity vulnerabilities.

pip-audit has no built-in --min-severity flag; OSV advisory data doesn't
always carry a CVSS score. This reads the JSON report pip-audit produces
and fails only on findings with a CVSS score >= 7.0 (High/Critical range).
Findings without a score are printed for visibility but do not fail CI.
"""
import json
import sys

HIGH_CRITICAL_THRESHOLD = 7.0


def get_cvss_score(vuln):
    severity = vuln.get("severity")

    if isinstance(severity, str):
        severity_name = severity.upper()
        if severity_name in {"HIGH", "CRITICAL"}:
            return HIGH_CRITICAL_THRESHOLD
        return None

    if isinstance(severity, dict):
        severity_type = severity.get("type", "").upper()
        if severity_type in ("CVSS_V3", "CVSS_V4"):
            try:
                return float(severity.get("score", 0))
            except (TypeError, ValueError):
                return None
        return None

    for entry in severity or []:
        if isinstance(entry, dict) and entry.get("type", "").upper() in ("CVSS_V3", "CVSS_V4"):
            try:
                return float(entry.get("score", 0))
            except (TypeError, ValueError):
                continue
    return None


def main(path):
    with open(path) as f:
        data = json.load(f)

    flagged = []
    unscored = []

    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            score = get_cvss_score(vuln)
            severity = vuln.get("severity")
            if isinstance(severity, str):
                severity_label = severity.upper()
            else:
                severity_label = "UNKNOWN"

            entry = (dep["name"], dep["version"], vuln["id"])
            if score is not None and score >= HIGH_CRITICAL_THRESHOLD:
                flagged.append((*entry, severity_label, score))
            elif score is None:
                unscored.append(entry)

    if unscored:
        print("Findings with no CVSS score (not blocking, review manually):")
        for name, version, vuln_id in unscored:
            print(f"  {name}=={version}  {vuln_id}")

    if flagged:
        print("\nHigh/Critical vulnerabilities found:")
        for name, version, vuln_id, severity_label, score in flagged:
            print(f"  {name}=={version}  {vuln_id}  {severity_label}  CVSS {score}")
        sys.exit(1)

    print("\nNo high or critical vulnerabilities found.")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1])