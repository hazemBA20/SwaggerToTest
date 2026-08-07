"""Generate runnable pytest API contract tests from an OpenAPI document.

The LLM produces a language-neutral test plan.  This program validates that
plan and deterministically renders it as pytest/httpx source code.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from config import API_BASE_URL, GOOGLE_API_KEY, GOOGLE_MODEL, GROQ_API_KEY, GROQ_MODEL, LLM_PROVIDER, TEST_RESET_PATH

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
LOGGER = logging.getLogger("swagger_to_test")


@dataclass
class TestCase:
    name: str
    method: str
    path: str
    expected_status: int
    kind: str = "positive"
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    payload: Any | None = None
    payload_form: dict[str, Any] | None = None
    operation_id: str = ""
    response_schema: dict[str, Any] | None = None
    setup: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DependencyEdge:
    producer_operation_id: str
    consumer_operation_id: str
    response_field: str
    path_parameter: str


@dataclass
class FlowStep:
    operation_id: str
    method: str
    path: str
    expected_status: int
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    payload: Any | None = None
    payload_form: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    extract: dict[str, str] = field(default_factory=dict)


@dataclass
class TestFlow:
    name: str
    kind: str
    isolation: dict[str, str]
    steps: list[FlowStep]


def test_to_dict(test: TestCase) -> dict[str, Any]:
    return {
        "name": test.name, "kind": test.kind, "method": test.method,
        "path": test.path, "query": test.query, "headers": test.headers,
        "payload": test.payload, "payload_form": test.payload_form, "expected_status": test.expected_status,
        "operation_id": test.operation_id, "response_schema": test.response_schema,
        "setup": test.setup,
    }


def flow_to_dict(flow: TestFlow) -> dict[str, Any]:
    return {
        "name": flow.name,
        "kind": flow.kind,
        "isolation": flow.isolation,
        "steps": [
            {
                "operation_id": step.operation_id,
                "method": step.method,
                "path": step.path,
                "query": step.query,
                "headers": step.headers,
                "payload": step.payload,
                "payload_form": step.payload_form,
                "expected_status": step.expected_status,
                "response_schema": step.response_schema,
                "extract": step.extract,
            }
            for step in flow.steps
        ],
    }


def load_spec(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        spec = json.load(file) if path.suffix.lower() == ".json" else yaml.safe_load(file)
    if not isinstance(spec, dict):
        raise ValueError("The OpenAPI document must be a JSON or YAML object.")
    try:
        from openapi_spec_validator import validate
        validate(spec, base_uri=path.resolve().as_uri())
    except ImportError:
        print("Warning: openapi-spec-validator is not installed; skipping validation.", file=sys.stderr)
    return spec


def resolve_ref(spec: dict[str, Any], value: Any) -> Any:
    """Resolve local JSON pointers; external references are deliberately rejected."""
    while isinstance(value, dict) and "$ref" in value:
        ref = value["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"External $ref is not supported in this demo: {ref}")
        current: Any = spec
        for segment in ref[2:].split("/"):
            current = current[segment.replace("~1", "/").replace("~0", "~")]
        value = current
    return value


def example_for_schema(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    schema = resolve_ref(spec, schema)
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
        return {name: example_for_schema(spec, props[name]) for name in fields}
    if typ == "array":
        return [example_for_schema(spec, schema.get("items", {}))]
    if typ == "integer":
        return max(1, schema.get("minimum", 1))
    if typ == "number":
        return schema.get("minimum", 1.0)
    if typ == "boolean":
        return True
    return "example"


def multipart_file_marker() -> dict[str, Any]:
    return {"__file__": True, "filename": "test.jpg", "content_type": "image/jpeg"}


def example_for_multipart_schema(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    schema = resolve_ref(spec, schema)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("format") == "binary":
        return multipart_file_marker()
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    typ = schema.get("type")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = list(schema.get("required", []))
        fields = list(required)
        for name, child_schema in props.items():
            if name in fields:
                continue
            child_schema = resolve_ref(spec, child_schema)
            if "default" in child_schema:
                fields.append(name)
        if not fields and schema.get("minProperties", 0) > 0 and props:
            fields = [next(iter(props))]
        return {name: example_for_multipart_schema(spec, props[name]) for name in fields}
    if typ == "array":
        return [example_for_multipart_schema(spec, schema.get("items", {}))]
    if typ == "integer":
        return max(1, schema.get("minimum", 1))
    if typ == "number":
        return schema.get("minimum", 1.0)
    if typ == "boolean":
        return True
    return "example"


def example_for_request_body(spec: dict[str, Any], operation: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    schema = operation["request_body_schema"]
    if not schema:
        return None, None
    if operation.get("request_body_media_type") == "multipart/form-data":
        payload_form = example_for_multipart_schema(spec, schema)
        if not isinstance(payload_form, dict):
            payload_form = {"value": payload_form}
        return None, payload_form
    return example_for_schema(spec, schema), None


def augment_multipart_defaults(spec: dict[str, Any], schema: dict[str, Any], payload_form: dict[str, Any]) -> dict[str, Any]:
    schema = resolve_ref(spec, schema)
    result = dict(payload_form)
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for name, child_schema in properties.items():
        if name in result:
            continue
        child_schema = resolve_ref(spec, child_schema)
        if "default" not in child_schema:
            continue
        result[name] = child_schema["default"]
    return result


def normalized_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    operations = []
    for path, path_item in spec.get("paths", {}).items():
        path_item = resolve_ref(spec, path_item)
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = resolve_ref(spec, operation)
            parameters = [resolve_ref(spec, p) for p in path_item.get("parameters", []) + operation.get("parameters", [])]
            request_body = resolve_ref(spec, operation.get("requestBody", {}))
            content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
            media_type = next(iter(content.keys()), None)
            body_schema = content.get(media_type, {}).get("schema") if media_type else None
            body_schema = resolve_ref(spec, body_schema) if body_schema else None
            operations.append({
                "operation_id": operation.get("operationId", f"{method}_{path}"),
                "method": method.upper(), "path": path, "summary": operation.get("summary", ""),
                "parameters": parameters, "request_body_media_type": media_type, "request_body_schema": body_schema,
                "responses": operation.get("responses", {}),
            })
    return operations


def success_status(operation: dict[str, Any]) -> int | None:
    statuses = [int(code) for code in operation["responses"] if code.isdigit() and 200 <= int(code) < 300]
    return min(statuses) if statuses else None


def schema_has_field(schema: dict[str, Any] | None, field_name: str) -> bool:
    if not schema:
        return False
    if field_name in schema.get("properties", {}):
        return True
    return any(schema_has_field(child, field_name) for child in schema.get("allOf", []))


def path_parameters(operation: dict[str, Any]) -> list[dict[str, Any]]:
    return [parameter for parameter in operation["parameters"] if parameter.get("in") == "path"]


def build_dependency_graph(spec: dict[str, Any], operations: list[dict[str, Any]]) -> list[DependencyEdge]:
    """Infer safe CRUD dependencies from POST response IDs and member path parameters."""
    edges: list[DependencyEdge] = []
    for producer in operations:
        if producer["method"] != "POST" or not success_status(producer):
            continue
        producer_schema = response_schema(spec, producer, success_status(producer))
        if not schema_has_field(producer_schema, "id"):
            continue
        for consumer in operations:
            if not consumer["path"].startswith(producer["path"] + "/{"):
                continue
            for parameter in path_parameters(consumer):
                parameter_name = parameter["name"]
                if parameter_name == "id" or parameter_name.endswith("_id"):
                    edges.append(DependencyEdge(
                        producer_operation_id=producer["operation_id"],
                        consumer_operation_id=consumer["operation_id"],
                        response_field="id",
                        path_parameter=parameter_name,
                    ))
    return edges


def graph_to_dict(edges: list[DependencyEdge]) -> dict[str, Any]:
    return {"edges": [edge.__dict__ for edge in edges]}


def step_for_operation(spec: dict[str, Any], operation: dict[str, Any], path: str, payload: Any | None = None,
                       extract: dict[str, str] | None = None) -> FlowStep:
    status = success_status(operation)
    if status is None:
        raise ValueError(f"Operation {operation['operation_id']} has no successful response.")
    payload_json = payload
    payload_form = None
    if payload is None and operation["request_body_schema"]:
        payload_json, payload_form = example_for_request_body(spec, operation)
    elif operation.get("request_body_media_type") == "multipart/form-data":
        payload_json = None
        payload_form = payload if isinstance(payload, dict) else {"value": payload}
    return FlowStep(
        operation_id=operation["operation_id"], method=operation["method"], path=path,
        expected_status=status, payload=payload_json, payload_form=payload_form, response_schema=response_schema(spec, operation, status),
        extract=extract or {},
    )


def deterministic_flows(spec: dict[str, Any], operations: list[dict[str, Any]], edges: list[DependencyEdge],
                        reset_path: str | None) -> list[TestFlow]:
    """Create CRUD lifecycle flows from validated graph edges for the demo scope."""
    if not reset_path:
        raise ValueError("Integration flows require --reset-path or TEST_RESET_PATH for isolation.")
    by_id = {operation["operation_id"]: operation for operation in operations}
    flows: list[TestFlow] = []
    for edge in edges:
        producer = by_id[edge.producer_operation_id]
        consumers = [by_id[candidate.consumer_operation_id] for candidate in edges
                     if candidate.producer_operation_id == producer["operation_id"]]
        by_method = {consumer["method"]: consumer for consumer in consumers}
        if not {"GET", "PATCH", "DELETE"}.issubset(by_method):
            continue
        binding = "created_resource_id"
        member_path = producer["path"] + "/{" + binding + "}"
        create_step = step_for_operation(spec, producer, producer["path"], extract={binding: edge.response_field})
        get_step = step_for_operation(spec, by_method["GET"], member_path)
        update_step = step_for_operation(spec, by_method["PATCH"], member_path)
        delete_step = step_for_operation(spec, by_method["DELETE"], member_path)
        isolation = {"kind": "reset", "path": reset_path}
        flows.append(TestFlow("crud_lifecycle", "lifecycle", isolation, [create_step, get_step, update_step, delete_step]))
        not_found = 404 if "404" in by_method["GET"]["responses"] else None
        if not_found:
            after_delete = FlowStep(
                operation_id=by_method["GET"]["operation_id"], method="GET", path=member_path,
                expected_status=not_found,
            )
            flows.append(TestFlow("deleted_resource_is_not_found", "lifecycle_negative", isolation,
                                  [create_step, delete_step, after_delete]))
        break
    return flows


def validate_flows(operations: list[dict[str, Any]], edges: list[DependencyEdge], flows: list[TestFlow]) -> None:
    operation_by_id = {operation["operation_id"]: operation for operation in operations}
    valid_edges = {(edge.producer_operation_id, edge.consumer_operation_id, edge.response_field) for edge in edges}
    for flow in flows:
        if flow.isolation.get("kind") != "reset" or not flow.isolation.get("path"):
            raise ValueError(f"Flow {flow.name} has no supported isolation strategy.")
        bindings: dict[str, tuple[str, str]] = {}
        for step in flow.steps:
            operation = operation_by_id.get(step.operation_id)
            if not operation or step.expected_status not in {int(code) for code in operation["responses"] if code.isdigit()}:
                raise ValueError(f"Flow {flow.name} uses an undocumented operation or status.")
            required_bindings = set(re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", step.path))
            if not required_bindings.issubset(bindings):
                raise ValueError(f"Flow {flow.name} uses a binding before it is extracted.")
            for binding in required_bindings:
                producer_operation_id, field = bindings[binding]
                if (producer_operation_id, step.operation_id, field) not in valid_edges:
                    raise ValueError(f"Flow {flow.name} uses a transition that is not in the dependency graph.")
            for binding, field in step.extract.items():
                if not schema_has_field(step.response_schema, field):
                    raise ValueError(f"Flow {flow.name} extracts an undocumented response field.")
                bindings[binding] = (step.operation_id, field)


def response_schema(spec: dict[str, Any], operation: dict[str, Any], status: int) -> dict[str, Any] | None:
    response = resolve_ref(spec, operation["responses"].get(str(status), {}))
    schema = response.get("content", {}).get("application/json", {}).get("schema")
    return dereference_schema(spec, schema) if schema else None


def dereference_schema(spec: dict[str, Any], value: Any) -> Any:
    if isinstance(value, dict):
        value = resolve_ref(spec, value)
        return {key: dereference_schema(spec, child) for key, child in value.items()}
    if isinstance(value, list):
        return [dereference_schema(spec, child) for child in value]
    return value


def first_error_status(operation: dict[str, Any], preferred: int | None = None) -> int | None:
    documented = {int(code) for code in operation["responses"] if code.isdigit() and 400 <= int(code) < 500}
    if preferred in documented:
        return preferred
    return min(documented) if documented else None


def attach_contract_metadata(spec: dict[str, Any], operation: dict[str, Any], tests: list[TestCase]) -> list[TestCase]:
    for test in tests:
        test.operation_id = operation["operation_id"]
        test.response_schema = response_schema(spec, operation, test.expected_status)
    return tests


def offline_plan(spec: dict[str, Any], operation: dict[str, Any]) -> list[TestCase]:
    """Conservative fallback based only on explicit spec facts."""
    path = operation["path"]
    query: dict[str, Any] = {}
    for parameter in operation["parameters"]:
        value = example_for_schema(spec, parameter.get("schema", {}))
        if parameter["in"] == "path":
            path = path.replace("{" + parameter["name"] + "}", str(value))
        elif parameter["in"] == "query" and parameter.get("required"):
            query[parameter["name"]] = value
    statuses = [int(s) for s in operation["responses"] if s.isdigit() and 200 <= int(s) < 300]
    if not statuses:
        return []
    payload, payload_form = example_for_request_body(spec, operation)
    identifier = safe_name(operation["operation_id"])
    tests = [TestCase(f"success", operation["method"], path, min(statuses), query=query, payload=payload, payload_form=payload_form)]
    schema = operation["request_body_schema"] or {}
    body_for_required = payload_form if payload_form is not None else payload
    for field_name in schema.get("required", []):
        if isinstance(body_for_required, dict):
            invalid = dict(body_for_required)
            invalid.pop(field_name, None)
            errors = [int(s) for s in operation["responses"] if s.isdigit() and 400 <= int(s) < 500]
            if errors:
                tests.append(TestCase(f"rejects_missing_{safe_name(field_name)}", operation["method"], path, min(errors), "negative", query=query, payload=None if payload_form is not None else invalid, payload_form=invalid if payload_form is not None else None))

    error = first_error_status(operation)
    if error and isinstance(body_for_required, dict):
        for field_name, field_schema in schema.get("properties", {}).items():
            field_schema = resolve_ref(spec, field_schema)
            if field_schema.get("enum"):
                invalid = dict(body_for_required)
                invalid[field_name] = "__invalid_enum_value__"
                tests.append(TestCase(f"rejects_invalid_{safe_name(field_name)}_enum", operation["method"], path, error, "negative", query=query, payload=None if payload_form is not None else invalid, payload_form=invalid if payload_form is not None else None))
        if schema.get("additionalProperties") is False:
            invalid = dict(body_for_required)
            invalid["unexpected_field"] = True
            tests.append(TestCase("rejects_unknown_body_field", operation["method"], path, error, "negative", query=query, payload=None if payload_form is not None else invalid, payload_form=invalid if payload_form is not None else None))
    if error:
        for parameter in operation["parameters"]:
            parameter_schema = resolve_ref(spec, parameter.get("schema", {}))
            if not parameter_schema.get("enum"):
                continue
            if parameter["in"] == "query":
                invalid_query = dict(query)
                invalid_query[parameter["name"]] = "__invalid_enum_value__"
                tests.append(TestCase(f"rejects_invalid_{safe_name(parameter['name'])}_query_enum", operation["method"], path, error, "negative", query=invalid_query, payload=payload, payload_form=payload_form))
            elif parameter["in"] == "header":
                invalid_headers = {parameter["name"]: "__invalid_enum_value__"}
                tests.append(TestCase(f"rejects_invalid_{safe_name(parameter['name'])}_header_enum", operation["method"], path, error, "negative", query=query, headers=invalid_headers, payload=payload, payload_form=payload_form))

    if 404 in {int(code) for code in operation["responses"] if code.isdigit()}:
        missing_path = path
        for parameter in operation["parameters"]:
            if parameter["in"] == "path":
                valid_value = example_for_schema(spec, parameter.get("schema", {}))
                missing_path = missing_path.replace(str(valid_value), "999999")
        tests.append(TestCase("returns_not_found_for_missing_resource", operation["method"], missing_path, 404, "negative", query=query, payload=payload, payload_form=payload_form))
    if operation["method"] == "POST" and 409 in {int(code) for code in operation["responses"] if code.isdigit()} and isinstance(body_for_required, dict):
        tests.append(TestCase(
            "rejects_duplicate_resource", operation["method"], path, 409, "stateful",
            query=query, payload=payload, payload_form=payload_form,
            setup=[{"method": "POST", "path": path, "query": query, "headers": {}, "payload": payload, "payload_form": payload_form, "expected_status": min(statuses)}],
        ))
    return attach_contract_metadata(spec, operation, tests)


def safe_name(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "operation"
    return f"operation_{result}" if result[0].isdigit() else result


def groq_plan(spec: dict[str, Any], operation: dict[str, Any], api_key: str, model: str) -> list[TestCase]:
    schema = llm_test_case_schema(operation)
    instructions = llm_instructions(operation)
    body = {"model": model, "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": json.dumps(operation)}], "response_format": {"type": "json_schema", "json_schema": {"name": "test_plan", "strict": True, "schema": schema}}, "temperature": 0}
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Some API edge protections reject Python's default urllib agent.
            "User-Agent": "SwaggerToTest/0.1 (OpenAPI contract-test generator)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            output = json.loads(response.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as error:
        response_body = error.read().decode(errors="replace")
        if error.code == 403 and "1010" in response_body:
            ray_id = error.headers.get("cf-ray", "not supplied")
            raise RuntimeError(
                "Groq rejected this request at its edge (HTTP 403 / error 1010), before the model ran. "
                "This is usually an IP/network or WAF restriction, not an OpenAPI, prompt, or test-generation error. "
                f"Cloudflare Ray ID: {ray_id}. Try a different network or contact Groq support with that Ray ID."
            ) from error
        raise RuntimeError(f"Groq API returned HTTP {error.code}: {response_body}") from error
    data = json.loads(output)
    return parse_llm_plan(data, spec, operation)


def parse_llm_plan(data: dict[str, Any], spec: dict[str, Any], operation: dict[str, Any]) -> list[TestCase]:
    """Validate provider-neutral structured output and convert it to test cases."""
    LOGGER.info("LLM test plan for %s:\n%s", operation["operation_id"], json.dumps(data, indent=2))
    tests: list[TestCase] = []
    schema = operation["request_body_schema"] or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required_names = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    required_binary_names = {
        name for name in required_names if isinstance(properties.get(name), dict) and properties[name].get("format") == "binary"
    }
    for item in data["tests"]:
        try:
            query = json.loads(item["query_json"])
            headers = json.loads(item["headers_json"])
            payload = json.loads(item["payload_json"])
            payload_form = json.loads(item["payload_form_json"]) if "payload_form_json" in item else None
        except (KeyError, json.JSONDecodeError) as error:
            raise RuntimeError(f"LLM returned an invalid JSON-encoded test value: {item}") from error
        if not isinstance(query, dict) or not isinstance(headers, dict):
            raise RuntimeError("LLM returned query_json or headers_json that is not a JSON object.")
        if operation.get("request_body_media_type") == "multipart/form-data":
            if payload is not None:
                raise RuntimeError("Multipart plans must set payload_json to null.")
            if not isinstance(payload_form, dict):
                raise RuntimeError("Multipart plans must provide payload_form_json as a JSON object.")
            # missing_required = required_names - set(payload_form)
            # if item.get("kind") == "negative" and missing_required and missing_required.isdisjoint(required_binary_names) and required_binary_names.issubset(payload_form):
            #     LOGGER.info(
            #         "Skipping multipart negative test %s because it only omits required text metadata fields: %s",
            #         item.get("name", "<unnamed>"),
            #         sorted(missing_required),
            #     )
            #     continue
            payload_form = augment_multipart_defaults(spec, operation["request_body_schema"] or {}, payload_form)
            tests.append(TestCase(name=item["name"], method=item["method"], path=item["path"],
                                  expected_status=item["expected_status"], kind=item["kind"],
                                  query=query, headers=headers, payload_form=payload_form))
        else:
            tests.append(TestCase(name=item["name"], method=item["method"], path=item["path"],
                                  expected_status=item["expected_status"], kind=item["kind"],
                                  query=query, headers=headers, payload=payload))
    return tests


def google_plan(spec: dict[str, Any], operation: dict[str, Any], api_key: str, model: str) -> list[TestCase]:
    """Call Google AI Studio's Gemini generateContent REST endpoint."""
    schema = llm_test_case_schema(operation)
    instructions = llm_instructions(operation)
    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(operation)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseJsonSchema": schema},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model, safe='')}:generateContent"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"x-goog-api-key": api_key, "Content-Type": "application/json", "User-Agent": "SwaggerToTest/0.1 (OpenAPI contract-test generator)"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        response_body = error.read().decode(errors="replace")
        raise RuntimeError(f"Google AI Studio returned HTTP {error.code}: {response_body}") from error
    try:
        output = "".join(part["text"] for part in result["candidates"][0]["content"]["parts"] if "text" in part)
        return parse_llm_plan(json.loads(output), spec, operation)
    except (KeyError, IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Google AI Studio returned no usable structured output: {result}") from error


def llm_plan(spec: dict[str, Any], operation: dict[str, Any], provider: str, model: str, api_key: str) -> list[TestCase]:
    if provider == "groq":
        return groq_plan(spec, operation, api_key, model)
    if provider == "google":
        return google_plan(spec, operation, api_key, model)
    raise ValueError("LLM_PROVIDER must be either 'groq' or 'google'.")


def llm_test_case_schema(operation: dict[str, Any]) -> dict[str, Any]:
    multipart = operation.get("request_body_media_type") == "multipart/form-data"
    properties: dict[str, Any] = {
        "name": {"type": "string"},
        "method": {"type": "string"},
        "path": {"type": "string"},
        "expected_status": {"type": "integer"},
        "kind": {"type": "string"},
        "query_json": {"type": "string"},
        "headers_json": {"type": "string"},
        "payload_json": {"type": "string"},
    }
    required = ["name", "method", "path", "expected_status", "kind", "query_json", "headers_json", "payload_json"]
    if multipart:
        properties["payload_form_json"] = {"type": "string"}
        required.append("payload_form_json")
    return {
        "type": "object",
        "properties": {
            "tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tests"],
        "additionalProperties": False,
    }

def llm_instructions(operation: dict[str, Any]) -> str:
    """Build the system prompt for one operation's LLM test plan."""
    is_multipart = operation.get("request_body_media_type") == "multipart/form-data"
    body_kind = "request-body schema (multipart/form-data)" if is_multipart else "request-body schema"

    core = (
        "You are generating a pytest-style API contract test plan for exactly one OpenAPI operation. "
        "Work in two passes internally, but return only the final JSON test list.\n\n"
        "PASS 1 - ENUMERATE (internal reasoning, not part of the output):\n"
        "List every testable signal explicitly present in the operation:\n"
        "  - each required field, in the body, query, path, or headers\n"
        "  - each field-level constraint on any field: enum, minLength, maxLength, pattern, "
        "minimum, maximum, multipleOf, format\n"
        "  - constraints stated only in free-text 'description' fields (e.g. allowed file types, "
        "allowed value ranges, units) - use these ONLY when both the constraint and a mapped "
        "error status are explicitly stated in the spec text\n"
        "  - every OPTIONAL field with a small, fixed value space - booleans, and enums/flags used "
        "as toggles - regardless of whether any distinct error status is documented for them. Do not "
        "skip these just because they are optional or unconstrained: 'optional' only means the field "
        "may be omitted, not that its documented values are untested. If the field has a schema "
        "'default', include the default explicitly as one of its values to exercise rather than "
        "relying on it being silently applied.\n"
        "  - every documented response status code, success and error\n"
        "For each signal, decide whether it can be exercised with a test grounded strictly in what "
        "the operation states. Skip any signal that would require inventing behavior.\n\n"
        "PASS 2 - GENERATE:\n"
        "Turn each testable signal from Pass 1 into one test case.\n"
        "  - Produce at least one positive test when a 2xx response is documented.\n"
        "  - Produce one negative test per independently-failing constraint (missing required field, "
        "invalid enum value, out-of-range/length/pattern value, etc), each asserting the specific "
        "documented error status that constraint maps to.\n"
        "  - For every optional boolean or toggle-style field identified in Pass 1, produce one "
        "positive test PER distinct value the field can take (e.g. true and false for a boolean), "
        "each asserting the SAME documented success status and response schema as the baseline "
        "positive test - unless the spec text documents a different status for a specific value, in "
        "which case assert that status instead. These are variation tests, not invented error tests: "
        "never assert a status code for a value combination unless that status is what the spec "
        "documents for the operation's success or the specific documented error path.\n"
        "  - Never combine two unrelated constraint violations in a single test case.\n\n"
        "HARD RULES (never break these):\n"
        f"  - Use only facts explicit in the operation: its parameters, its {body_kind}, and its "
        "documented responses. Do not invent fields, auth, endpoints, status codes, or constraints.\n"
        "  - Do not invent NEW error statuses for optional fields that have no documented error "
        "mapping. Varying such a field's value must always assert an already-documented status "
        "(normally the operation's existing success status).\n"
        # "  - Do not invent rejection behavior for fields that may be echoed, optional in practice, or "
        # "otherwise not visibly validated by a documented response.\n"
        # "  - If in doubt about whether the API actually enforces something, omit the test rather than guess."
    )

    if is_multipart:
        format_rules = (
            "\n\nMULTIPART FORMAT:\n"
            "  - Set payload_json to null; put the form body in payload_form_json.\n"
            "  - Required file fields (format: binary) must be JSON objects with __file__, filename, "
            "and content_type.\n"
            "  - Include optional multipart fields when they have schema defaults, using those default values.\n"
            "  - For a boolean/toggle field with a schema default, generate one positive test using the "
            "default value (if not already covered by the baseline test) and one positive test using "
            "the other value, each asserting the documented success status.\n"
            "  - For rejection tests, omit required binary file fields before omitting required text "
            "metadata fields, unless the description or documented responses clearly show the text "
            "field itself is validated with a 4xx.\n"
            "  - If a file field's description states allowed formats/types (e.g. 'JPEG, PNG, PDF') and a "
            "documented error response covers invalid files, you may vary content_type/filename to "
            "represent an unsupported type for one negative test."
        )
    else:
       format_rules = (
            "\n\nMULTIPART FORMAT:\n"
            "  - Set payload_json to null; put the form body in payload_form_json.\n"
            "  - Required file fields (format: binary) must be JSON objects with __file__, filename, "
            "and content_type.\n"
            "  - Include optional multipart fields when they have schema defaults, using those default values.\n"
            "  - For a boolean/toggle field with a schema default, generate one positive test using the "
            "default value (if not already covered by the baseline test) and one positive test using "
            "the other value, each asserting the documented success status.\n"
            "  - Generate one missing-field negative test per field listed in the body schema's "
            "'required' list, binary or text, each omitting only that one field and asserting the "
            "documented 4xx. A required field is enforced by definition unless its description states "
            "otherwise - do not skip one required field's test because another required field already "
            "has one.\n"
            "  - Separately, if a file field's description states allowed formats/types (e.g. 'JPEG, "
            "PNG, PDF') and a documented error response covers invalid files, add one additional "
            "negative test with a mismatched filename/content_type representing an unsupported type. "
            "This is independent of the missing-field tests above."
        )

    return core + format_rules + "\n\nReturn JSON only."

def render_request_call(
    method: str,
    path_expr: str,
    query_expr: str,
    headers_expr: str,
    payload_expr: str | None = None,
    payload_form_expr: str | None = None,
    response_name: str = "response",
    indent: str = "    ",
) -> list[str]:
    lines: list[str] = []
    if payload_form_expr is not None:
        lines.append(f"{indent}_files, _data = split_multipart({payload_form_expr})")
        lines.extend(
            [
                f"{indent}{response_name} = client.request(",
                f"{indent}    {method!r},",
                f"{indent}    {path_expr},",
                f"{indent}    params={query_expr},",
                f"{indent}    headers={headers_expr},",
                f"{indent}    files=_files or None,",
                f"{indent}    data=_data or None,",
                f"{indent})",
            ]
        )
        return lines
    if payload_expr is not None:
        lines.extend(
            [
                f"{indent}{response_name} = client.request(",
                f"{indent}    {method!r},",
                f"{indent}    {path_expr},",
                f"{indent}    params={query_expr},",
                f"{indent}    headers={headers_expr},",
                f"{indent}    json={payload_expr},",
                f"{indent})",
            ]
        )
        return lines
    lines.extend(
        [
            f"{indent}{response_name} = client.request(",
            f"{indent}    {method!r},",
            f"{indent}    {path_expr},",
            f"{indent}    params={query_expr},",
            f"{indent}    headers={headers_expr},",
            f"{indent})",
        ]
    )
    return lines


def render(tests: list[TestCase], base_url: str, reset_path: str | None, flows: list[TestFlow] | None = None) -> str:
    names: set[str] = set()
    function_names: list[str] = []
    for test in tests:
        function_name = f"test_{safe_name(test.operation_id)}_{safe_name(test.name)}"
        if function_name in names:
            raise ValueError(f"Duplicate generated test function name: {function_name}")
        names.add(function_name)
        function_names.append(function_name)
        lines = ["\"\"\"Generated by openapi_to_tests.py. Do not edit generated output.\"\"\"", "import os", "import httpx", "import pytest", "", f"BASE_URL = os.getenv(\"API_BASE_URL\", {base_url!r}).rstrip(\"/\")", f"TEST_RESET_PATH = os.getenv(\"TEST_RESET_PATH\", {reset_path!r})", "", "def assert_json_matches_schema(value, schema, location='response'):", "    for child_schema in schema.get('allOf', []):", "        assert_json_matches_schema(value, child_schema, location)", "    if 'enum' in schema:", "        assert value in schema['enum'], f'{location} is not an allowed enum value: {value!r}'", "    expected_type = schema.get('type')", "    type_checks = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float), 'boolean': bool}", "    if expected_type in type_checks and value is not None:", "        assert isinstance(value, type_checks[expected_type]), f'{location} should be {expected_type}'", "    if expected_type == 'object' or 'properties' in schema:", "        required_names = set(schema.get('required', []))", "        for name in required_names:", "            assert name in value and value[name] is not None, f'{location} is missing required field {name}'", "        for name, child_schema in schema.get('properties', {}).items():", "            if name not in value:", "                continue", "            child_value = value[name]", "            if child_value is None and name not in required_names:", "                continue", "            assert_json_matches_schema(child_value, child_schema, f'{location}.{name}')", "    if expected_type == 'array' and 'items' in schema:", "        for index, item in enumerate(value):", "            assert_json_matches_schema(item, schema['items'], f'{location}[{index}]')", "", "def resolve_bindings(value, bindings):", "    if isinstance(value, str):", "        return value.format_map(bindings)", "    if isinstance(value, list):", "        return [resolve_bindings(item, bindings) for item in value]", "    if isinstance(value, dict):", "        return {key: resolve_bindings(item, bindings) for key, item in value.items()}", "    return value", "", "@pytest.fixture", "def client():", "    with httpx.Client(base_url=BASE_URL, timeout=10.0) as api_client:", "        if TEST_RESET_PATH:", "            reset_response = api_client.post(TEST_RESET_PATH)", "            assert reset_response.status_code == 204, f'Could not reset test data: {reset_response.status_code}'", "        yield api_client", ""]
    lines.extend([
        "",
        "from pathlib import Path",
        "TEST_DATA_DIR = os.getenv(\"TEST_DATA_DIR\", \"carte_grise\")",
        "",
        "def split_multipart(payload_form: dict) -> tuple[dict, dict]:",
        "    files = {}",
        "    data = {}",
        "    for key, value in (payload_form or {}).items():",
        "        if isinstance(value, dict) and value.get('__file__'):",
        "            filename = value.get('filename', 'test.bin')",
        "            content_type = value.get('content_type', 'application/octet-stream')",
        "            test_data_path = Path(TEST_DATA_DIR)",
        "            if test_data_path.exists():",
        "                image_files = list(test_data_path.rglob('*.jpg')) + list(test_data_path.rglob('*.png')) + list(test_data_path.rglob('*.jpeg'))",
        "                if image_files:",
        "                    real_file_path = image_files[0]",
        "                    with open(real_file_path, 'rb') as file_handle:",
        "                        file_content = file_handle.read()",
        "                    files[key] = (filename, file_content, content_type)",
        "                else:",
        "                    files[key] = (filename, b'test-content', content_type)",
        "            else:",
        "                files[key] = (filename, b'test-content', content_type)",
        "        else:",
        "            data[key] = value",
        "    return files, data",
        "",
    ])
    for test, function_name in zip(tests, function_names):
        lines.append(f"def {function_name}(client):")
        for setup in test.setup:
            setup_payload_expr = repr(setup["payload"]) if setup.get("payload") is not None else None
            setup_payload_form_expr = repr(setup["payload_form"]) if setup.get("payload_form") is not None else None
            lines.extend(
                render_request_call(
                    setup["method"],
                    repr(setup["path"]),
                    repr(setup["query"]),
                    repr(setup["headers"]),
                    setup_payload_expr,
                    setup_payload_form_expr,
                    response_name="setup_response",
                )
            )
            lines.append(f"    assert setup_response.status_code == {setup['expected_status']}")
        test_payload_expr = repr(test.payload) if test.payload is not None else None
        test_payload_form_expr = repr(test.payload_form) if test.payload_form is not None else None
        lines.extend(
            render_request_call(
                test.method,
                repr(test.path),
                repr(test.query),
                repr(test.headers),
                test_payload_expr,
                test_payload_form_expr,
            )
        )
        lines.append(f"    assert response.status_code == {test.expected_status}")
        if test.response_schema:
            lines.append(f"    assert_json_matches_schema(response.json(), {test.response_schema!r})")
        lines.append("")
    for flow in flows or []:
        lines += [f"def test_flow_{safe_name(flow.name)}(client):", "    bindings = {}"]
        for step in flow.steps:
            step_payload_expr = f"resolve_bindings({step.payload!r}, bindings)" if step.payload is not None else None
            step_payload_form_expr = f"resolve_bindings({step.payload_form!r}, bindings)" if step.payload_form is not None else None
            lines.extend(
                render_request_call(
                    step.method,
                    f"resolve_bindings({step.path!r}, bindings)",
                    f"resolve_bindings({step.query!r}, bindings)",
                    f"resolve_bindings({step.headers!r}, bindings)",
                    step_payload_expr,
                    step_payload_form_expr,
                )
            )
            lines.append(f"    assert response.status_code == {step.expected_status}")
            if step.response_schema:
                lines.append(f"    assert_json_matches_schema(response.json(), {step.response_schema!r})")
            for binding, field_name in step.extract.items():
                lines.append(f"    bindings[{binding!r}] = response.json()[{field_name!r}]")
        lines.append("")
    return "\n".join(lines)


def karate_matchers(schema: dict[str, Any]) -> dict[str, str]:
    """Return Karate type matchers for directly declared response object fields."""
    result: dict[str, str] = {}
    for child in schema.get("allOf", []):
        result.update(karate_matchers(child))
    type_map = {"string": "#string", "integer": "#number", "number": "#number", "boolean": "#boolean", "array": "#[]", "object": "#object"}
    for name in schema.get("required", []):
        child = schema.get("properties", {}).get(name, {})
        matcher = type_map.get(child.get("type"))
        if matcher:
            result[name] = matcher
    return result


def karate_path(path: str) -> str:
    parts = []
    for part in path.strip("/").split("/"):
        if not part:
            continue
        match = re.fullmatch(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", part)
        parts.append(match.group(1) if match else json.dumps(part))
    return ", ".join(parts) or "''"


def render_karate_request(lines: list[str], method: str, path: str, query: dict[str, Any], headers: dict[str, str], payload: Any) -> None:
    lines += ["  Given url baseUrl", f"  And path {karate_path(path)}"]
    if query:
        lines.append(f"  And params {json.dumps(query)}")
    if headers:
        lines.append(f"  And headers {json.dumps(headers)}")
    if payload is not None:
        lines.append(f"  And request {json.dumps(payload)}")
    lines.append(f"  When method {method.lower()}")


def render_karate(tests: list[TestCase], base_url: str, reset_path: str | None, flows: list[TestFlow] | None = None) -> str:
    scenario_names: set[str] = set()
    lines = ["# Generated by openapi_to_tests.py. Do not edit generated output.", "Feature: OpenAPI contract tests", "", "Background:", f"  * def baseUrl = karate.properties['api.baseUrl'] || {json.dumps(base_url)}"]
    if reset_path:
        lines += ["  Given url baseUrl", f"  And path {karate_path(reset_path)}", "  When method post", "  Then status 204"]
    for test in tests:
        scenario_name = f"{test.operation_id}: {test.name}"
        if scenario_name in scenario_names:
            raise ValueError(f"Duplicate generated Karate scenario name: {scenario_name}")
        scenario_names.add(scenario_name)
        tags = f"@operation_{safe_name(test.operation_id)} @{safe_name(test.kind)}"
        lines += ["", tags, f"Scenario: {scenario_name}"]
        for setup in test.setup:
            render_karate_request(lines, setup["method"], setup["path"], setup["query"], setup["headers"], setup["payload"])
            lines.append(f"  Then status {setup['expected_status']}")
        render_karate_request(lines, test.method, test.path, test.query, test.headers, test.payload)
        lines.append(f"  Then status {test.expected_status}")
        if test.response_schema:
            matchers = karate_matchers(test.response_schema)
            if matchers:
                match_object = ", ".join(f"{json.dumps(key)}: {json.dumps(value)}" for key, value in matchers.items())
                lines.append(f"  And match response contains {{ {match_object} }}")
    for flow in flows or []:
        lines += ["", f"@flow @{safe_name(flow.kind)}", f"Scenario: flow: {flow.name}"]
        for step in flow.steps:
            render_karate_request(lines, step.method, step.path, step.query, step.headers, step.payload)
            lines.append(f"  Then status {step.expected_status}")
            for binding, field_name in step.extract.items():
                lines.append(f"  * def {binding} = response.{field_name}")
    return "\n".join(lines) + "\n"


def render_karate_config(base_url: str) -> str:
    return "// Generated by openapi_to_tests.py.\nfunction fn() {\n  var baseUrl = karate.properties['api.baseUrl'] || " + json.dumps(base_url) + ";\n  return { baseUrl: baseUrl };\n}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pytest and/or Karate contract tests from OpenAPI.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("generated_tests/test_generated_api.py"))
    parser.add_argument("--target", choices=["pytest", "karate", "both"], default="pytest", help="Test framework output to generate.")
    parser.add_argument("--karate-out", type=Path, default=Path("generated_tests/karate/api.feature"), help="Output .feature file for the Karate target.")
    parser.add_argument("--plan-out", type=Path, default=None, help="Where to write the JSON test plan.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--reset-path", default=TEST_RESET_PATH, help="Optional endpoint called before every generated test.")
    parser.add_argument("--llm", action="store_true", help="Use the configured LLM provider to make the test plan.")
    parser.add_argument("--llm-only", action="store_true", help="Use only the LLM test plan and skip deterministic fallback tests.")
    parser.add_argument("--integration", action="store_true", help="Generate deterministic CRUD integration flows from the dependency graph.")
    parser.add_argument("--integration-only", action="store_true", help="Generate only integration flows in separate default output files.")
    parser.add_argument("--graph-out", type=Path, default=None, help="Where to write the inferred dependency graph JSON.")
    parser.add_argument("--flow-plan-out", type=Path, default=None, help="Where to write the integration flow plan JSON.")
    parser.add_argument("--provider", choices=["groq", "google"], default=LLM_PROVIDER, help="Overrides LLM_PROVIDER from .env.")
    parser.add_argument("--model", default=None, help="Overrides GROQ_MODEL or GOOGLE_MODEL from .env.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    if args.integration_only:
        if args.llm:
            parser.error("--llm cannot be combined with --integration-only in this deterministic demo.")
        args.integration = True
        if args.out == Path("generated_tests/test_generated_api.py"):
            args.out = Path("generated_tests/test_integration.py")
        if args.karate_out == Path("generated_tests/karate/api.feature"):
            args.karate_out = Path("generated_tests/karate/integration.feature")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    spec = load_spec(args.spec)
    operations = normalized_operations(spec)
    base_url = args.base_url or os.getenv("API_BASE_URL") or spec.get("servers", [{}])[0].get("url", API_BASE_URL)
    all_tests: list[TestCase] = []
    provider = args.provider
    key = GROQ_API_KEY if provider == "groq" else GOOGLE_API_KEY
    model = args.model or (GROQ_MODEL if provider == "groq" else GOOGLE_MODEL)
    if args.llm and not args.integration_only:
        LOGGER.info("Using LLM provider=%s model=%s", provider, model)
    if not args.integration_only:
        for operation in operations:
            LOGGER.info("Processing %s %s (%s)", operation["method"], operation["path"], operation["operation_id"])
            if args.llm:
                if not key:
                    variable = "GROQ_API_KEY" if provider == "groq" else "GOOGLE_API_KEY"
                    raise SystemExit(f"--llm with LLM_PROVIDER={provider} requires the {variable} environment variable.")
                llm_tests = attach_contract_metadata(spec, operation, llm_plan(spec, operation, provider, model, key))
                all_tests.extend(llm_tests)
                if not args.llm_only:
                    # The LLM adds breadth; deterministic cases guarantee explicit constraints are covered.
                    all_tests.extend(offline_plan(spec, operation))
            else:
                planned = offline_plan(spec, operation)
                LOGGER.info("Deterministic test plan for %s:\n%s", operation["operation_id"], json.dumps([test_to_dict(test) for test in planned], indent=2))
                all_tests.extend(planned)
    flows: list[TestFlow] = []
    edges: list[DependencyEdge] = []
    if args.integration:
        edges = build_dependency_graph(spec, operations)
        flows = deterministic_flows(spec, operations, edges, args.reset_path)
        validate_flows(operations, edges, flows)
        if not flows:
            raise SystemExit("No supported CRUD integration flows could be derived from this specification.")
        LOGGER.info("Generated %d deterministic integration flows from %d graph edges.", len(flows), len(edges))
    if not all_tests and not flows:
        raise SystemExit("No runnable tests could be derived from this specification.")
    if args.target in {"pytest", "both"}:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(all_tests, base_url, args.reset_path, flows), encoding="utf-8")
        print(f"Generated {len(all_tests)} pytest tests and {len(flows)} integration flows: {args.out}")
    if args.target in {"karate", "both"}:
        args.karate_out.parent.mkdir(parents=True, exist_ok=True)
        args.karate_out.write_text(render_karate(all_tests, base_url, args.reset_path, flows), encoding="utf-8")
        config_path = args.karate_out.parent / "karate-config.js"
        config_path.write_text(render_karate_config(base_url), encoding="utf-8")
        print(f"Generated {len(all_tests)} Karate scenarios and {len(flows)} integration flows: {args.karate_out}")
        print(f"Generated Karate configuration: {config_path}")
    plan_out = args.plan_out or (args.karate_out.with_suffix(".plan.json") if args.target == "karate" else args.out.with_suffix(".plan.json"))
    plan_out.write_text(json.dumps([test_to_dict(test) for test in all_tests], indent=2) + "\n", encoding="utf-8")
    print(f"Saved test plan: {plan_out}")
    if args.integration:
        graph_out = args.graph_out or plan_out.with_name(plan_out.stem + ".graph.json")
        flow_plan_out = args.flow_plan_out or plan_out.with_name(plan_out.stem + ".flows.json")
        graph_out.write_text(json.dumps(graph_to_dict(edges), indent=2) + "\n", encoding="utf-8")
        flow_plan_out.write_text(json.dumps([flow_to_dict(flow) for flow in flows], indent=2) + "\n", encoding="utf-8")
        print(f"Saved dependency graph: {graph_out}")
        print(f"Saved integration flow plan: {flow_plan_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
