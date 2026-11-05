"""Keep every finding in evidence; block High/Critical vulnerabilities or any secret."""

import json
from pathlib import Path
import sys


def blocking_findings(report: dict) -> list[dict]:
    if report.get("SchemaVersion") != 2 or not isinstance(report.get("Results"), list):
        raise ValueError("Missing or unsupported Trivy results")
    findings = []
    for result in report["Results"]:
        findings.extend(item for item in result.get("Vulnerabilities", [])
                        if item.get("Severity") in {"HIGH", "CRITICAL"})
        findings.extend(result.get("Secrets", []))
    return findings


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text())
    blocked = blocking_findings(data)
    if blocked:
        print(f"Release rejected: {len(blocked)} blocking security findings; see retained report")
        sys.exit(1)
    print("PASS: no High/Critical vulnerabilities or detected secrets; full report retained")
