import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cvss import CVSS4

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_audit_severity.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_audit_severity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Required so dataclasses/typing can resolve `from __future__ import
    # annotations` hints against the module's own namespace on Python 3.12+.
    # Without this, importlib.util.module_from_spec() + exec_module() alone
    # leaves the module absent from sys.modules, and dataclass field
    # resolution fails with "'NoneType' object has no attribute '__dict__'".
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_audit_script_uses_osv_severity_when_report_omits_it(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "audit.json"
    input_path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "jinja2",
                        "version": "3.1.4",
                        "vulns": [
                            {
                                "id": "PYSEC-2026-1471",
                                "fix_versions": ["3.1.6"],
                                "aliases": ["GHSA-cpwx-vrp4-4pq7", "CVE-2025-27516"],
                                "description": "example advisory",
                            }
                        ],
                    }
                ]
            }
        )
    )

    module = _load_script_module()

    # A real CVSS v4.0 vector string, as OSV actually returns it -- the
    # leading "CVSS:4.0" segment is the spec version, NOT the score. The
    # base score below is computed from the vector via the same `cvss`
    # library the script uses, so this test verifies the script's parsed
    # score actually matches, rather than just asserting *some* score
    # crossed the threshold. (An earlier version of this fixture used the
    # invalid vector "CVSS:8.0/...", which let a regex-based parsing bug
    # that read "8.0" as the score pass silently.)
    real_vector = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
    expected_score = CVSS4(real_vector).base_score

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "aliases": ["GHSA-cpwx-vrp4-4pq7", "CVE-2025-27516"],
                    "severity": [
                        {
                            "type": "CVSS_V4",
                            "score": real_vector,
                        }
                    ],
                }
            ).encode()

    def fake_urlopen(url, timeout=10):
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exc_info:
        module.main(str(input_path))

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "High/Critical vulnerabilities found" in captured.out
    assert f"CVSS {expected_score}" in captured.out


def test_audit_script_fails_when_high_or_critical_vulnerabilities_exist(tmp_path):
    input_path = tmp_path / "audit.json"
    input_path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "urllib3",
                        "version": "1.26.0",
                        "vulns": [
                            {
                                "id": "PYSEC-2024-1",
                                "fix_versions": ["1.26.1"],
                                "severity": "HIGH"
                            }
                        ],
                    }
                ]
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(input_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "HIGH" in result.stdout


def test_audit_script_passes_when_only_low_or_none_vulnerabilities_exist(tmp_path):
    input_path = tmp_path / "audit.json"
    input_path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "urllib3",
                        "version": "1.26.0",
                        "vulns": [
                            {
                                "id": "PYSEC-2024-2",
                                "fix_versions": ["1.26.1"],
                                "severity": "LOW"
                            }
                        ],
                    }
                ]
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(input_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "No high or critical vulnerabilities" in result.stdout