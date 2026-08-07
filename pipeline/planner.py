from __future__ import annotations

import os
from typing import Any

from pipeline.flows import build_flows_for_state
from pipeline.llm_provider import get_llm
from pipeline.models import PipelineState, TestCase, TestCasePlan, ValidationError
from pipeline.validator import operation_by_id

SYSTEM_PROMPT = """You generate conservative API contract test plans from one OpenAPI operation.

Rules:
- Use only facts present in the ResolvedOperation JSON. Do not invent fields, paths, auth headers, or status codes.
- expected_status must be one of documented_statuses.
- Do not include auth headers (Authorization, API keys) — those are injected at render time.
- Produce one positive test per documented 2xx status code.
- Add negative tests only for constraints explicit in the schema (required fields, enums).
- If media_type is application/json, use payload_json only (payload_form must be null).
- If media_type is multipart/form-data, use payload_form only (payload_json must be null).
  CRITICAL: Include ALL required fields in payload_form.
  For required file fields (format: binary), the value MUST be a JSON object with three keys:
    "__file__": true (boolean)
    "filename": "test.jpg" (string with appropriate extension)
    "content_type": "image/jpeg" (string with appropriate MIME type)
  EXAMPLE: "registrationDocument": {"__file__": true, "filename": "test.jpg", "content_type": "image/jpeg"}
  NEVER use a string value like "__file__" - always use the full JSON object structure.
  For required string fields use concrete example values from the schema.
  For optional fields, include them if they have default values or are commonly used.
- Substitute concrete values for path parameters in path strings.
- For each test, include the operation_id field from the input. Omit the id field (it will be assigned automatically).
"""


def operations_to_plan(state: PipelineState) -> list[str]:
    if state.pending_plan_operations:
        return list(state.pending_plan_operations)
    return [op.operation_id for op in state.operations]


def retry_reasons(state: PipelineState, operation_id: str) -> list[str]:
    test_ids = {t.id for t in state.plan if t.operation_id == operation_id}
    return [
        err.reason
        for err in state.validation_errors
        if err.test_id in test_ids
    ]


def plan_operation(
    operation,
    provider: str,
    model: str,
    retry_reasons_list: list[str] | None = None,
) -> list[TestCase]:
    llm = get_llm(provider, model, temperature=0)
    structured = llm.with_structured_output(TestCasePlan)
    user_content = operation.model_dump_json()
    if retry_reasons_list:
        user_content += (
            "\n\nPrevious plan failed validation. Fix these issues:\n"
            + "\n".join(f"- {reason}" for reason in retry_reasons_list)
        )
    result: TestCasePlan = structured.invoke(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
    )
    for test in result.tests:
        test.operation_id = operation.operation_id
    return result.tests


def plan_node(state: PipelineState) -> dict[str, Any]:
    if not state.flows and state.operations:
        flows = build_flows_for_state(state)
    else:
        flows = state.flows

    target_ops = operations_to_plan(state)
    ops = operation_by_id(state)
    existing = [t for t in state.plan if t.operation_id not in target_ops]

    provider = getattr(state, "provider", "anthropic")
    new_tests: list[TestCase] = []
    for operation_id in target_ops:
        operation = ops.get(operation_id)
        if not operation:
            continue
        reasons = retry_reasons(state, operation_id) if state.validation_errors else []
        planned = plan_operation(operation, provider, state.model, reasons or None)
        new_tests.extend(planned)

    return {
        "flows": flows,
        "plan": existing + new_tests,
        "validation_errors": [],
        "pending_plan_operations": [],
    }


def plan_node_skip_llm(state: PipelineState) -> dict[str, Any]:
    """Used when ANTHROPIC_API_KEY is missing in unit tests."""
    return plan_node(state)
