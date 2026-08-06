from __future__ import annotations

from typing import Any

from langchain_anthropic import ChatAnthropic

from pipeline.models import Patch, PatchPlan, PipelineState, TestCase
from pipeline.validator import operation_by_id

REVISE_SYSTEM = """You fix failing generated API tests by emitting field-level patches only.

Rules:
- Output patches that modify only the TestCase fields needed to fix the failure.
- Allowed fields include: name, kind, method, path, query, headers, payload_json, payload_form, expected_status.
- Do not change test id.
- Use payload_form with __file__ markers for multipart file fields when needed.
- Do not weaken assertions to match a broken API — only fix generation mistakes.
"""


def reviser_node(state: PipelineState) -> dict[str, Any]:
    ops = operation_by_id(state)
    tests_by_id = {test.id: test for test in state.plan}
    bug_ids = {t.test_id for t in state.triage if t.category == "generation_bug"}

    llm = ChatAnthropic(model=state.model, temperature=0)
    structured = llm.with_structured_output(PatchPlan)

    updated = list(state.plan)
    for triage in state.triage:
        if triage.category != "generation_bug":
            continue
        test = tests_by_id.get(triage.test_id)
        if not test:
            continue
        operation = ops[test.operation_id]
        prompt = (
            f"Operation:\n{operation.model_dump_json()}\n\n"
            f"Test:\n{test.model_dump_json()}\n\n"
            f"Failure detail:\n{triage.detail}"
        )
        plan: PatchPlan = structured.invoke(
            [
                {"role": "system", "content": REVISE_SYSTEM},
                {"role": "user", "content": prompt},
            ]
        )
        apply_patches(test, plan.patches)

    return {"plan": updated}


def apply_patches(test: TestCase, patches: list[Patch]) -> None:
    allowed = set(TestCase.model_fields.keys()) - {"id", "revision"}
    for patch in patches:
        if patch.test_id != test.id:
            continue
        if patch.field not in allowed:
            continue
        setattr(test, patch.field, patch.new_value)
    test.revision += 1
