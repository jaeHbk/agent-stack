from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_security.py"
SPEC = importlib.util.spec_from_file_location("stack_security_audit", SCRIPT)
assert SPEC and SPEC.loader
security_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security_audit)


def report(findings: list[dict], scanned: int = 96) -> dict:
    return {
        "total_components_scanned": scanned,
        "finding_count": len(findings),
        "findings": findings,
    }


def finding(vuln_id: str, package: str = "hermes-agent") -> dict:
    return {
        "package": package,
        "version": "0.19.0" if package == "hermes-agent" else "1.0.0",
        "ecosystem": "PyPI",
        "source": "venv",
        "vuln_id": vuln_id,
        "severity": "MODERATE",
        "summary": "test advisory",
        "fixed_versions": [],
    }


class AuditEvaluationTests(unittest.TestCase):
    def test_clean_report_passes(self) -> None:
        accepted, blocking = security_audit.evaluate_report(
            report([]), allow_source_remediated=True
        )
        self.assertEqual(accepted, [])
        self.assertEqual(blocking, [])

    def test_exact_source_remediations_are_accepted(self) -> None:
        items = [finding(vuln_id) for vuln_id in security_audit.SOURCE_REMEDIATED]
        accepted, blocking = security_audit.evaluate_report(
            report(items), allow_source_remediated=True
        )
        self.assertEqual(accepted, items)
        self.assertEqual(blocking, [])

    def test_exception_is_not_implicit(self) -> None:
        item = finding("GHSA-pmqc-57g8-c22c")
        accepted, blocking = security_audit.evaluate_report(
            report([item]), allow_source_remediated=False
        )
        self.assertEqual(accepted, [])
        self.assertEqual(blocking, [item])

    def test_any_other_finding_blocks(self) -> None:
        item = finding("GHSA-new-advisory", package="dependency")
        accepted, blocking = security_audit.evaluate_report(
            report([item]), allow_source_remediated=True
        )
        self.assertEqual(accepted, [])
        self.assertEqual(blocking, [item])

    def test_zero_component_report_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive component count"):
            security_audit.evaluate_report(
                report([], scanned=0), allow_source_remediated=True
            )

    def test_mismatched_count_fails_closed(self) -> None:
        malformed = report([])
        malformed["finding_count"] = 1
        with self.assertRaisesRegex(ValueError, "finding_count"):
            security_audit.evaluate_report(
                malformed, allow_source_remediated=True
            )


if __name__ == "__main__":
    unittest.main()
