from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import black
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.models import PipelineState, ResolvedOperation, TestCase


def safe_name(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "operation"
    if result[0].isdigit():
        result = f"operation_{result}"
    return result


def prepare_test_context(
    test: TestCase,
    operation: ResolvedOperation,
) -> dict[str, Any]:
    use_multipart = operation.media_type == "multipart/form-data"
    response_schema = operation.response_schemas.get(test.expected_status)
    function_name = f"{safe_name(test.operation_id)}_{safe_name(test.name)}"
    return {
        "id": test.id,
        "function_name": function_name,
        "method": test.method,
        "path": test.path,
        "query": test.query,
        "headers": test.headers,
        "payload_json": test.payload_json,
        "payload_form": test.payload_form,
        "expected_status": test.expected_status,
        "use_multipart": use_multipart,
        "response_schema": response_schema,
    }


def prepare_flow_context(flow, operations: dict[str, ResolvedOperation]) -> dict[str, Any]:
    steps = []
    for step in flow.steps:
        operation = operations[step.operation_id]
        steps.append(
            {
                "method": step.method,
                "path": step.path,
                "payload_json": step.payload_json,
                "expected_status": step.expected_status,
                "response_schema": operation.response_schemas.get(step.expected_status),
                "extract": step.extract,
            }
        )
    return {
        "name": flow.name,
        "function_name": safe_name(flow.name),
        "steps": steps,
    }


def render_tests(state: PipelineState) -> str:
    ops = {op.operation_id: op for op in state.operations}
    tests_ctx = [prepare_test_context(test, ops[test.operation_id]) for test in state.plan]
    flows_ctx = [prepare_flow_context(flow, ops) for flow in state.flows]

    auth_headers = {
        key: value for key, value in state.auth_config.items() if value is not None
    }

    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("test_template.py.jinja2")
    source = template.render(
        base_url=state.base_url,
        reset_path=state.reset_path or "",
        auth_headers=auth_headers,
        tests=tests_ctx,
        flows=flows_ctx,
    )
    return black.format_str(source, mode=black.Mode())


def render_node(state: PipelineState) -> dict[str, Any]:
    code = render_tests(state)
    output_path = Path(state.code_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(code, encoding="utf-8")
    return {"code_path": str(output_path)}
