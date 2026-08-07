from __future__ import annotations

import os
from typing import Any

from pipeline.models import (
    DependencyEdge,
    FlowStep,
    PipelineState,
    ResolvedOperation,
    TestFlow,
)


def success_status(operation: ResolvedOperation) -> int | None:
    statuses = [s for s in operation.documented_statuses if 200 <= s < 300]
    return min(statuses) if statuses else None


def schema_has_field(schema: dict[str, Any] | None, field_name: str) -> bool:
    if not schema:
        return False
    if field_name in schema.get("properties", {}):
        return True
    return any(schema_has_field(child, field_name) for child in schema.get("allOf", []))


def build_dependency_graph(operations: list[ResolvedOperation]) -> list[DependencyEdge]:
    edges: list[DependencyEdge] = []
    by_id = {op.operation_id: op for op in operations}
    for producer in operations:
        if producer.method != "POST":
            continue
        status = success_status(producer)
        if status is None:
            continue
        producer_schema = producer.response_schemas.get(status)
        if not schema_has_field(producer_schema, "id"):
            continue
        for consumer in operations:
            prefix = producer.path + "/{"
            if not consumer.path.startswith(prefix):
                continue
            for parameter in consumer.path_parameters:
                parameter_name = parameter["name"]
                if parameter_name == "id" or parameter_name.endswith("_id"):
                    edges.append(
                        DependencyEdge(
                            producer_operation_id=producer.operation_id,
                            consumer_operation_id=consumer.operation_id,
                            response_field="id",
                            path_parameter=parameter_name,
                        )
                    )
    return edges


def example_for_schema(schema: dict[str, Any]) -> Any:
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    typ = schema.get("type")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        fields = required or (list(props)[:1] if schema.get("minProperties", 0) > 0 else [])
        return {name: example_for_schema(props[name]) for name in fields}
    if typ == "array":
        return [example_for_schema(schema.get("items", {}))]
    if typ == "integer":
        return max(1, schema.get("minimum", 1))
    if typ == "number":
        return schema.get("minimum", 1.0)
    if typ == "boolean":
        return True
    return "example"


def step_for_operation(
    operation: ResolvedOperation,
    path: str,
    payload_json: dict | None = None,
    extract: dict[str, str] | None = None,
) -> FlowStep:
    status = success_status(operation)
    if status is None:
        raise ValueError(f"Operation {operation.operation_id} has no successful response.")
    if payload_json is None and operation.request_schema and operation.media_type == "application/json":
        payload_json = example_for_schema(operation.request_schema)
    return FlowStep(
        operation_id=operation.operation_id,
        method=operation.method,
        path=path,
        expected_status=status,
        payload_json=payload_json,
        extract=extract or {},
    )


def deterministic_flows(
    operations: list[ResolvedOperation],
    edges: list[DependencyEdge],
    reset_path: str | None,
) -> list[TestFlow]:
    if not reset_path:
        return []
    by_id = {operation.operation_id: operation for operation in operations}
    flows: list[TestFlow] = []
    for edge in edges:
        producer = by_id[edge.producer_operation_id]
        consumers = [
            by_id[candidate.consumer_operation_id]
            for candidate in edges
            if candidate.producer_operation_id == producer.operation_id
        ]
        by_method = {consumer.method: consumer for consumer in consumers}
        if not {"GET", "PATCH", "DELETE"}.issubset(by_method):
            continue
        binding = "created_resource_id"
        member_path = producer.path + "/{" + binding + "}"
        create_step = step_for_operation(
            producer, producer.path, extract={binding: edge.response_field}
        )
        get_step = step_for_operation(by_method["GET"], member_path)
        update_step = step_for_operation(by_method["PATCH"], member_path)
        delete_step = step_for_operation(by_method["DELETE"], member_path)
        flows.append(
            TestFlow(
                name="crud_lifecycle",
                steps=[create_step, get_step, update_step, delete_step],
                reset_path=reset_path,
            )
        )
        get_op = by_method["GET"]
        not_found = 404 if 404 in get_op.documented_statuses else None
        if not_found:
            after_delete = FlowStep(
                operation_id=get_op.operation_id,
                method="GET",
                path=member_path,
                expected_status=not_found,
            )
            flows.append(
                TestFlow(
                    name="deleted_resource_is_not_found",
                    steps=[create_step, delete_step, after_delete],
                    reset_path=reset_path,
                )
            )
        break
    return flows


def build_flows_for_state(state: PipelineState) -> list[TestFlow]:
    reset_path = state.reset_path or os.getenv("TEST_RESET_PATH") or None
    edges = build_dependency_graph(state.operations)
    return deterministic_flows(state.operations, edges, reset_path)
