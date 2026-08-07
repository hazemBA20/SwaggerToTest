from __future__ import annotations

from pathlib import Path

from pipeline.models import PipelineState, TestCase, TriageResult
from pipeline.validator import operation_by_id


def format_finding(test: TestCase | None, triage: TriageResult, operation_name: str) -> str:
    lines = [
        f"### {operation_name} — test `{test.name if test else triage.test_id}`",
        f"- **Category:** {triage.category}",
        f"- **Detail:** {triage.detail}",
    ]
    if test:
        lines.append(f"- **Expected status:** {test.expected_status}")
    return "\n".join(lines)


def report_node(state: PipelineState) -> dict[str, object]:
    ops = operation_by_id(state)
    tests_by_id = {t.id: t for t in state.plan}

    counts: dict[str, int] = {}
    for item in state.triage:
        counts[item.category] = counts.get(item.category, 0) + 1

    lines = [
        "# API contract test report",
        "",
        "## Summary",
        "",
    ]
    for category in ("passed", "environment", "generation_bug", "real_bug"):
        if category in counts:
            lines.append(f"- **{category}:** {counts[category]}")

    all_findings = list(state.findings)
    seen = {f.test_id for f in all_findings}
    for item in state.triage:
        if item.category == "real_bug" and item.test_id not in seen:
            all_findings.append(item)

    if all_findings:
        lines.extend(["", "## Contract violations (real_bug)", ""])
        for finding in all_findings:
            test = tests_by_id.get(finding.test_id)
            op_name = test.operation_id if test else "unknown"
            operation = ops.get(op_name)
            label = operation.summary or op_name if operation else op_name
            lines.append(format_finding(test, finding, label))
            lines.append("")

    if state.dropped_test_ids:
        lines.extend(["", "## Dropped tests (validation retry cap)", ""])
        for test_id in state.dropped_test_ids:
            test = tests_by_id.get(test_id)
            name = test.name if test else test_id
            lines.append(f"- {name} (`{test_id}`)")

    content = "\n".join(lines).strip() + "\n"
    if state.report_path:
        path = Path(state.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {}
