"""Generate runnable pytest API contract tests from an OpenAPI document.

The LLM produces a language-neutral test plan.  This program validates that
plan and deterministically renders it as pytest/httpx source code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


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
        return {name: example_for_schema(spec, prop) for name, prop in props.items() if name in required}
    if typ == "array":
        return [example_for_schema(spec, schema.get("items", {}))]
    if typ == "integer":
        return max(1, schema.get("minimum", 1))
    if typ == "number":
        return schema.get("minimum", 1.0)
    if typ == "boolean":
        return True
    return "example"


def normalized_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    operations = []
    for path, path_item in spec.get("paths", {}).items():
        path_item = resolve_ref(spec, path_item)
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = resolve_ref(spec, operation)
            parameters = [resolve_ref(spec, p) for p in path_item.get("parameters", []) + operation.get("parameters", [])]
            content = resolve_ref(spec, operation.get("requestBody", {})).get("content", {})
            body_schema = content.get("application/json", {}).get("schema")
            body_schema = resolve_ref(spec, body_schema) if body_schema else None
            operations.append({
                "operation_id": operation.get("operationId", f"{method}_{path}"),
                "method": method.upper(), "path": path, "summary": operation.get("summary", ""),
                "parameters": parameters, "request_body_schema": body_schema,
                "responses": operation.get("responses", {}),
            })
    return operations


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
    payload = example_for_schema(spec, operation["request_body_schema"]) if operation["request_body_schema"] else None
    identifier = safe_name(operation["operation_id"])
    tests = [TestCase(f"test_{identifier}_success", operation["method"], path, min(statuses), query=query, payload=payload)]
    schema = operation["request_body_schema"] or {}
    for field_name in schema.get("required", []):
        if isinstance(payload, dict):
            invalid = dict(payload)
            invalid.pop(field_name, None)
            errors = [int(s) for s in operation["responses"] if s.isdigit() and 400 <= int(s) < 500]
            if errors:
                tests.append(TestCase(f"test_{identifier}_rejects_missing_{safe_name(field_name)}", operation["method"], path, min(errors), "negative", query=query, payload=invalid))
    return tests


def safe_name(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "operation"
    return f"operation_{result}" if result[0].isdigit() else result


def groq_plan(operation: dict[str, Any], api_key: str, model: str) -> list[TestCase]:
    schema = {"type": "object", "properties": {"tests": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "method": {"type": "string"}, "path": {"type": "string"}, "expected_status": {"type": "integer"}, "kind": {"type": "string"}, "query": {"type": "object"}, "headers": {"type": "object"}, "payload": {}}, "required": ["name", "method", "path", "expected_status"], "additionalProperties": False}}}, "required": ["tests"], "additionalProperties": False}
    instructions = ("Generate conservative API contract test plans from one OpenAPI operation. "
                    "Use only facts explicit in the operation. Do not invent fields, auth, endpoints, or status codes. "
                    "Create one happy-path test only if a 2xx response is documented, and negative tests only for explicit constraints. Return JSON only.")
    body = {"model": model, "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": json.dumps(operation)}], "response_format": {"type": "json_schema", "json_schema": {"name": "test_plan", "strict": True, "schema": schema}}, "temperature": 0}
    request = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            output = json.loads(response.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Groq API returned HTTP {error.code}: {error.read().decode()}") from error
    data = json.loads(output)
    return [TestCase(**item) for item in data["tests"]]


def render(tests: list[TestCase], base_url: str) -> str:
    lines = ["\"\"\"Generated by openapi_to_tests.py. Do not edit generated output.\"\"\"", "import os", "import httpx", "import pytest", "", f"BASE_URL = os.getenv(\"API_BASE_URL\", {base_url!r}).rstrip(\"/\")", "", "@pytest.fixture", "def client():", "    with httpx.Client(base_url=BASE_URL, timeout=10.0) as api_client:", "        yield api_client", ""]
    for test in tests:
        lines += [f"def {safe_name(test.name)}(client):", f"    response = client.request({test.method!r}, {test.path!r}, params={test.query!r}, headers={test.headers!r}, json={test.payload!r})", f"    assert response.status_code == {test.expected_status}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pytest contract tests from OpenAPI.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("generated_tests/test_generated_api.py"))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--llm", action="store_true", help="Use Groq to make the test plan; requires GROQ_API_KEY.")
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    args = parser.parse_args()
    spec = load_spec(args.spec)
    base_url = args.base_url or spec.get("servers", [{}])[0].get("url", "http://localhost:8000")
    all_tests: list[TestCase] = []
    key = os.getenv("GROQ_API_KEY")
    for operation in normalized_operations(spec):
        if args.llm:
            if not key:
                raise SystemExit("--llm requires the GROQ_API_KEY environment variable.")
            all_tests.extend(groq_plan(operation, key, args.model))
        else:
            all_tests.extend(offline_plan(spec, operation))
    if not all_tests:
        raise SystemExit("No runnable tests could be derived from this specification.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(all_tests, base_url), encoding="utf-8")
    print(f"Generated {len(all_tests)} runnable pytest tests: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
