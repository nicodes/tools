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


def image_scan_findings(report, sbom, *, context, input_archive=None, data_archive=None):
    """Admit complete image evidence without weakening the standalone parser."""
    from image_scan_context import admit_empty, authenticate_archive, validate
    inspected = validate(report, sbom, context)
    if 'archive' in context:
        authenticate_archive(input_archive, context, inspected)
    elif input_archive is not None:
        raise ValueError('offline input requires an exact archive completion context')
    if 'Results' not in report or report['Results'] == []:
        admit_empty(report, sbom, context, data_archive)
        return []
    if not isinstance(report['Results'], list):
        raise ValueError('image scan result inventory must be a list')
    for result in report['Results']:
        if (not isinstance(result, dict) or not isinstance(result.get('Target'), str) or not result['Target']
                or result.get('Class') not in {'os-pkgs', 'lang-pkgs', 'secret'}):
            raise ValueError('image scan result has no supported actual target and scanner class')
        for name in ['Vulnerabilities', 'Secrets']:
            if name in result and (not isinstance(result[name], list) or any(not isinstance(item, dict) for item in result[name])):
                raise ValueError('image scan findings must be complete lists of records')
        if any(item.get('Severity') not in {'UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}
               for item in result.get('Vulnerabilities', [])):
            raise ValueError('image vulnerability has missing or unsupported severity')
    return blocking_findings(report)


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text())
    blocked = blocking_findings(data)
    if blocked:
        print(f"Release rejected: {len(blocked)} blocking security findings; see retained report")
        sys.exit(1)
    print("PASS: no High/Critical vulnerabilities or detected secrets; full report retained")
