import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_audit_severity.py"


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
    )

    assert result.returncode == 0
    assert "No high or critical vulnerabilities" in result.stdout
