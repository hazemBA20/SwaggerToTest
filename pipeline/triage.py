from __future__ import annotations

from typing import Any

from langchain_anthropic import ChatAnthropic

from pipeline.models import (
    Patch,
    PatchPlan,
    PipelineState,
    ResolvedOperation,
    TestCase,
    TriageDecision,
    TriageResult,
)
from pipeline.validator import operation_by_id

TRIAGE_SYSTEM = """Classify a failing API contract test into exactly one category:
- environment: harness misconfiguration (missing auth token, 401/403 when auth is required)
- generation_bug: the generated test is wrong (bad payload, wrong media type, malformed request)
- real_bug: request was well-formed but the API response violates its OpenAPI contract

Return one category and a one-sentence reason."""


def test_uses_missing_auth(test: TestCase, state: PipelineState, operation: ResolvedOperation) -> bool:
    for header in operation.security_headers_required:
        if state.auth_config.get(header) is None:
            return True
    return False


def triage_one(
    test: TestCase,
    operation: ResolvedOperation,
    exec_result,
    state: PipelineState,
    model: str,
) -> TriageResult:
    if exec_result.passed:
        return TriageResult(test_id=test.id, category="passed", detail="Test passed")

    status = exec_result.status_code
    if test_uses_missing_auth(test, state, operation):
        return TriageResult(
            test_id=test.id,
            category="environment",
            detail="Required auth credential missing from environment",
        )
    if status in (401, 403) and operation.security_headers_required:
        return TriageResult(
            test_id=test.id,
            category="environment",
            detail=f"Received {status} while auth is required by the spec",
        )

    if operation.media_type == "multipart/form-data" and test.kind == "positive":
        schema = operation.request_schema or {}
        required = schema.get("required", [])
        form = test.payload_form or {}
        for field in required:
            prop = (schema.get("properties") or {}).get(field, {})
            if prop.get("format") == "binary" and field not in form:
                return TriageResult(
                    test_id=test.id,
                    category="generation_bug",
                    detail=f"Required file field {field!r} missing from payload_form",
                )

    if status and 400 <= status < 500:
        if test.kind == "negative" and status == test.expected_status:
            return TriageResult(
                test_id=test.id,
                category="passed",
                detail="Negative test received expected error status",
            )
        if test.kind == "positive":
            return TriageResult(
                test_id=test.id,
                category="generation_bug",
                detail=f"Unexpected client error {status} for positive test",
            )

    if status is not None and status != test.expected_status:
        if test.kind == "positive" and status in operation.documented_statuses:
            return TriageResult(
                test_id=test.id,
                category="real_bug",
                detail=f"Expected status {test.expected_status}, got {status}",
            )
        if test.kind == "positive":
            return TriageResult(
                test_id=test.id,
                category="real_bug",
                detail=f"Expected status {test.expected_status}, got {status}",
            )

    detail = exec_result.traceback or "Test failed without traceback"
    if "jsonschema" in detail.lower() or "validationerror" in detail.lower():
        return TriageResult(
            test_id=test.id,
            category="real_bug",
            detail="Response body failed JSON Schema validation against the documented contract",
        )

    return llm_triage(test, operation, detail, model)


def llm_triage(
    test: TestCase,
    operation: ResolvedOperation,
    failure_detail: str,
    model: str,
) -> TriageResult:
    llm = ChatAnthropic(model=model, temperature=0)
    structured = llm.with_structured_output(TriageDecision)
    prompt = (
        f"Operation:\n{operation.model_dump_json()}\n\n"
        f"Test:\n{test.model_dump_json()}\n\n"
        f"Failure:\n{failure_detail}"
    )
    decision: TriageDecision = structured.invoke(
        [
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    )
    return TriageResult(test_id=test.id, category=decision.category, detail=decision.detail)


def triage_node(state: PipelineState) -> dict[str, Any]:
    ops = operation_by_id(state)
    tests_by_id = {test.id: test for test in state.plan}
    results_by_id = {result.test_id: result for result in state.exec_results}

    triage: list[TriageResult] = []
    new_findings = list(state.findings)

    for test_id, exec_result in results_by_id.items():
        test = tests_by_id.get(test_id)
        if not test:
            continue
        operation = ops[test.operation_id]
        result = triage_one(test, operation, exec_result, state, state.model)
        triage.append(result)
        if result.category == "real_bug":
            new_findings.append(result)

    for test in state.plan:
        if test.id not in results_by_id:
            triage.append(
                TriageResult(
                    test_id=test.id,
                    category="generation_bug",
                    detail="No pytest result correlated to test_id",
                )
            )

    return {"triage": triage, "findings": new_findings}


def route_after_triage(state: PipelineState) -> str:
    generation_bugs = [t for t in state.triage if t.category == "generation_bug"]
    if generation_bugs and state.iteration < state.max_iterations:
        return "revise"
    return "report"


def increment_iteration(state: PipelineState) -> dict[str, Any]:
    return {"iteration": state.iteration + 1}
