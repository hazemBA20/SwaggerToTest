from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pipeline.models import ExecutedTestResult, PipelineState

TEST_ID_PATTERN = re.compile(r"test_id:\s*([0-9a-f-]{36})", re.IGNORECASE)
STATUS_PATTERN = re.compile(r"STATUS:(\d+)")


def extract_test_id(nodeid: str, report_entry: dict) -> str | None:
    for source in (
        report_entry.get("metadata", {}).get("docstring", ""),
        report_entry.get("docstring", ""),
        report_entry.get("setup", {}).get("longrepr", ""),
    ):
        if source:
            match = TEST_ID_PATTERN.search(str(source))
            if match:
                return match.group(1)
    call = report_entry.get("call", {})
    longrepr = call.get("longrepr", "") or ""
    match = TEST_ID_PATTERN.search(str(longrepr))
    if match:
        return match.group(1)
    stdout = call.get("stdout", "") or ""
    match = TEST_ID_PATTERN.search(stdout)
    if match:
        return match.group(1)
    return None


def extract_status_code(report_entry: dict) -> int | None:
    call = report_entry.get("call", {})
    for chunk in (call.get("stdout", ""), call.get("longrepr", ""), call.get("stderr", "")):
        if not chunk:
            continue
        match = STATUS_PATTERN.search(str(chunk))
        if match:
            return int(match.group(1))
    return None


def parse_pytest_json_report(report_path: Path) -> list[ExecutedTestResult]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    results: list[ExecutedTestResult] = []
    for test in data.get("tests", []):
        test_id = extract_test_id(test.get("nodeid", ""), test)
        if not test_id:
            continue
        outcome = test.get("outcome", "failed")
        passed = outcome == "passed"
        status_code = extract_status_code(test)
        traceback = None
        if not passed:
            call = test.get("call", {})
            traceback = call.get("longrepr") or call.get("crash", {}).get("message")
        results.append(
            ExecutedTestResult(
                test_id=test_id,
                passed=passed,
                status_code=status_code,
                traceback=str(traceback) if traceback else None,
            )
        )
    return results


def execute_node(state: PipelineState) -> dict[str, object]:
    if not state.code_path:
        raise ValueError("code_path is required before execute")

    with tempfile.TemporaryDirectory() as tmp:
        report_file = Path(tmp) / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            state.code_path,
            "--json-report",
            f"--json-report-file={report_file}",
            "-v",
        ]
        subprocess.run(cmd, check=False, capture_output=True, text=True)
        if not report_file.exists():
            return {"exec_results": []}
        results = parse_pytest_json_report(report_file)
    return {"exec_results": results}
