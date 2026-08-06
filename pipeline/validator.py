from __future__ import annotations

import re
from typing import Any

from pipeline.flows import build_dependency_graph
from pipeline.models import (
    DependencyEdge,
    PipelineState,
    ResolvedOperation,
    TestCase,
    TestFlow,
    ValidationError,
)

PLAN_RETRY_CAP = 2


def operation_by_id(state: PipelineState) -> dict[str, ResolvedOperation]:
    return {op.operation_id: op for op in state.operations}


def path_pattern_matches(concrete_path: str, template_path: str) -> bool:
    pattern = re.sub(r"\{[^}]+\}", r"[^/]+", template_path)
    return re.fullmatch(pattern, concrete_path) is not None


def is_missing_field_negative(test: TestCase, schema: dict | None) -> bool:
    if test.kind != "negative" or not schema:
        return False
    name = test.name.lower()
    return "missing" in name or "rejects_missing" in name


def is_enum_violation_negative(test: TestCase) -> bool:
    if test.kind != "negative":
        return False
    name = test.name.lower()
    return "invalid" in name and "enum" in name


def validate_payload_keys(
    test: TestCase,
    schema: dict | None,
    payload: dict | None,
    payload_label: str,
) -> list[str]:
    errors: list[str] = []
    if payload is None:
        return errors
    if not schema:
        if payload:
            errors.append(f"{payload_label} provided but operation has no body schema")
        return errors

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for key in payload:
        if key.startswith("__"):
            continue
        if isinstance(payload.get(key), dict) and payload[key].get("__file__"):
            prop = properties.get(key, {})
            if prop.get("format") != "binary" and prop.get("type") != "string":
                errors.append(f"Field {key} marked as file but schema is not binary")
            continue
        if key not in properties:
            errors.append(f"Unknown field {key!r} in {payload_label}")

    if test.kind == "positive" or not is_missing_field_negative(test, schema):
        for field in required:
            if field not in payload:
                errors.append(f"Required field {field!r} missing from {payload_label}")

    for field_name, field_schema in properties.items():
        if field_name not in payload:
            continue
        value = payload[field_name]
        if isinstance(value, dict) and value.get("__file__"):
            continue
        enum_values = field_schema.get("enum")
        if enum_values and value not in enum_values and not is_enum_violation_negative(test):
            errors.append(f"Field {field_name!r} value {value!r} not in enum {enum_values}")

    return errors


def validate_test_case(test: TestCase, operation: ResolvedOperation) -> list[str]:
    errors: list[str] = []
    if test.method.upper() != operation.method.upper():
        errors.append(f"Method {test.method} does not match operation method {operation.method}")
    if not path_pattern_matches(test.path, operation.path):
        errors.append(f"Path {test.path!r} does not match operation path {operation.path!r}")
    if test.expected_status not in operation.documented_statuses:
        errors.append(
            f"expected_status {test.expected_status} not in documented statuses "
            f"{operation.documented_statuses}"
        )

    if operation.media_type == "application/json":
        if test.payload_form is not None:
            errors.append("payload_form set for JSON operation")
        errors.extend(
            validate_payload_keys(test, operation.request_schema, test.payload_json, "payload_json")
        )
    elif operation.media_type == "multipart/form-data":
        if test.payload_json is not None:
            errors.append("payload_json set for multipart operation")
        form_errors = validate_payload_keys(
            test, operation.request_schema, test.payload_form, "payload_form"
        )
        errors.extend(form_errors)
        if test.kind == "positive" and operation.request_schema:
            required = operation.request_schema.get("required", [])
            props = operation.request_schema.get("properties", {})
            form = test.payload_form or {}
            for field in required:
                prop = props.get(field, {})
                is_file = prop.get("format") == "binary"
                if field not in form:
                    errors.append(f"Required multipart field {field!r} missing from payload_form")
                elif is_file:
                    marker = form.get(field)
                    if not isinstance(marker, dict) or not marker.get("__file__"):
                        errors.append(
                            f"Required file field {field!r} must use __file__ marker in payload_form"
                        )
    else:
        if test.payload_json or test.payload_form:
            errors.append("Payload provided for operation without request body")

    return errors


def validate_flows(
    operations: list[ResolvedOperation],
    edges: list[DependencyEdge],
    flows: list[TestFlow],
) -> list[str]:
    errors: list[str] = []
    operation_by_id = {operation.operation_id: operation for operation in operations}
    valid_edges = {
        (edge.producer_operation_id, edge.consumer_operation_id, edge.response_field)
        for edge in edges
    }
    for flow in flows:
        if not flow.reset_path:
            errors.append(f"Flow {flow.name} has no reset path configured")
        bindings: dict[str, tuple[str, str]] = {}
        for step in flow.steps:
            operation = operation_by_id.get(step.operation_id)
            if not operation:
                errors.append(f"Flow {flow.name} references unknown operation {step.operation_id}")
                continue
            if step.expected_status not in operation.documented_statuses:
                errors.append(
                    f"Flow {flow.name} step uses undocumented status {step.expected_status}"
                )
            required_bindings = set(re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", step.path))
            if not required_bindings.issubset(bindings):
                errors.append(f"Flow {flow.name} uses a binding before it is extracted")
            for binding in required_bindings:
                producer_operation_id, field = bindings[binding]
                if (producer_operation_id, step.operation_id, field) not in valid_edges:
                    errors.append(f"Flow {flow.name} transition not backed by dependency graph")
            response_schema = operation.response_schemas.get(step.expected_status)
            for binding, field in step.extract.items():
                if not schema_has_field(response_schema, field):
                    errors.append(f"Flow {flow.name} extracts undocumented field {field!r}")
                bindings[binding] = (step.operation_id, field)
    return errors


def schema_has_field(schema: dict[str, Any] | None, field_name: str) -> bool:
    if not schema:
        return False
    if field_name in schema.get("properties", {}):
        return True
    return any(schema_has_field(child, field_name) for child in schema.get("allOf", []))


def validate_plan(state: PipelineState) -> dict[str, Any]:
    ops = operation_by_id(state)
    validation_errors: list[ValidationError] = []
    edges = build_dependency_graph(state.operations)
    for flow_error in validate_flows(state.operations, edges, state.flows):
        validation_errors.append(ValidationError(test_id="", reason=f"flow: {flow_error}"))

    valid_tests: list[TestCase] = []
    for test in state.plan:
        if test.id in state.dropped_test_ids:
            continue
        operation = ops.get(test.operation_id)
        if not operation:
            validation_errors.append(
                ValidationError(test_id=test.id, reason=f"Unknown operation_id {test.operation_id}")
            )
            continue
        case_errors = validate_test_case(test, operation)
        if case_errors:
            for reason in case_errors:
                validation_errors.append(ValidationError(test_id=test.id, reason=reason))
        else:
            valid_tests.append(test)

    pending_operations: list[str] = []
    plan_retries = dict(state.plan_retries)
    dropped = list(state.dropped_test_ids)

    failures_by_operation: dict[str, list[ValidationError]] = {}
    for err in validation_errors:
        if not err.test_id:
            continue
        test = next((t for t in state.plan if t.id == err.test_id), None)
        if not test:
            continue
        failures_by_operation.setdefault(test.operation_id, []).append(err)

    for operation_id, _errors in failures_by_operation.items():
        retries = plan_retries.get(operation_id, 0)
        if retries < PLAN_RETRY_CAP:
            plan_retries[operation_id] = retries + 1
            pending_operations.append(operation_id)
        else:
            failed_ids = {e.test_id for e in _errors}
            for test in state.plan:
                if test.operation_id == operation_id and test.id in failed_ids:
                    if test.id not in dropped:
                        dropped.append(test.id)

    if pending_operations:
        kept = [t for t in valid_tests if t.operation_id not in pending_operations]
        return {
            "plan": kept,
            "validation_errors": validation_errors,
            "plan_retries": plan_retries,
            "pending_plan_operations": pending_operations,
            "dropped_test_ids": dropped,
        }

    return {
        "plan": valid_tests,
        "validation_errors": validation_errors,
        "plan_retries": plan_retries,
        "pending_plan_operations": [],
        "dropped_test_ids": dropped,
    }


def route_after_validate(state: PipelineState) -> str:
    if state.pending_plan_operations:
        return "retry_plan"
    return "render"
