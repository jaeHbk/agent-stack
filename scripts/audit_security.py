#!/usr/bin/env python3
"""Fail closed on Hermes OSV findings with two source-remediation exceptions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERMES_VERSION = "0.19.0"
SOURCE_REMEDIATED = {
    "GHSA-pmqc-57g8-c22c": "vendored upstream Feishu webhook fix 46c1cfec",
    "GHSA-xq8w-9jvx-gm3v": "pinned source contains cited compression fix 2ca38e5d",
}


def evaluate_report(
    report: dict[str, Any], *, allow_source_remediated: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings = report.get("findings")
    scanned = report.get("total_components_scanned")
    reported_count = report.get("finding_count")
    if not isinstance(scanned, int) or scanned < 1:
        raise ValueError("audit did not report a positive component count")
    if not isinstance(findings, list):
        raise ValueError("audit findings are missing or malformed")
    if reported_count != len(findings):
        raise ValueError("audit finding_count does not match the findings list")

    accepted: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("audit contains a malformed finding")
        is_source_remediated = (
            allow_source_remediated
            and finding.get("package") == "hermes-agent"
            and finding.get("version") == HERMES_VERSION
            and finding.get("ecosystem") == "PyPI"
            and finding.get("source") == "venv"
            and finding.get("vuln_id") in SOURCE_REMEDIATED
        )
        (accepted if is_source_remediated else blocking).append(finding)
    return accepted, blocking


def _finding_line(finding: dict[str, Any]) -> str:
    return "{} {}=={} {}: {}".format(
        finding.get("severity", "UNKNOWN"),
        finding.get("package", "unknown"),
        finding.get("version", "unknown"),
        finding.get("vuln_id", "unknown"),
        finding.get("summary", ""),
    ).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--allow-source-remediated", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("audit root must be an object")
        accepted, blocking = evaluate_report(
            report, allow_source_remediated=args.allow_source_remediated
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FAIL  invalid Hermes security audit: {error}", file=sys.stderr)
        return 2

    print(
        f"Security audit scanned {report['total_components_scanned']} components "
        f"and returned {len(report['findings'])} finding(s)."
    )
    for finding in accepted:
        vuln_id = str(finding["vuln_id"])
        print(f"ACCEPTED  {_finding_line(finding)}")
        print(f"          {SOURCE_REMEDIATED[vuln_id]}")
    for finding in blocking:
        print(f"BLOCKING  {_finding_line(finding)}", file=sys.stderr)

    if blocking:
        print(
            f"FAIL  {len(blocking)} unremediated security finding(s)",
            file=sys.stderr,
        )
        return 1
    print("PASS  no unremediated Hermes security findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
