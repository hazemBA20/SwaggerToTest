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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from config import API_BASE_URL, GROQ_API_KEY, GROQ_MODEL

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


def test_to_dict(test: TestCase) -> dict[str, Any]:
    return {
        "name": test.name, "kind": test.kind, "method": test.method,
        "path": test.path, "query": test.query, "headers": test.headers,
        "payload": test.payload, "expected_status": test.expected_status,
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
    # Strict structured output disallows arbitrary-key objects. Keep those
    # flexible values as JSON strings, then parse them below.
    schema = {"type": "object", "properties": {"tests": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "method": {"type": "string"}, "path": {"type": "string"}, "expected_status": {"type": "integer"}, "kind": {"type": "string"}, "query_json": {"type": "string"}, "headers_json": {"type": "string"}, "payload_json": {"type": "string"}}, "required": ["name", "method", "path", "expected_status", "kind", "query_json", "headers_json", "payload_json"], "additionalProperties": False}}}, "required": ["tests"], "additionalProperties": False}
    instructions = ("Generate conservative API contract test plans from one OpenAPI operation. "
                    "Use only facts explicit in the operation. Do not invent fields, auth, endpoints, or status codes. "
                    "Create one happy-path test only if a 2xx response is documented, and negative tests only for explicit constraints. "
                    "query_json, headers_json, and payload_json must each be valid JSON encoded as a string; use '{}' for empty query/headers and 'null' for no body. Return JSON only.")
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
    LOGGER.info("LLM test plan for %s:\n%s", operation["operation_id"], json.dumps(data, indent=2))
    tests: list[TestCase] = []
    for item in data["tests"]:
        try:
            query = json.loads(item["query_json"])
            headers = json.loads(item["headers_json"])
            payload = json.loads(item["payload_json"])
        except (KeyError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Groq returned an invalid JSON-encoded test value: {item}") from error
        if not isinstance(query, dict) or not isinstance(headers, dict):
            raise RuntimeError("Groq returned query_json or headers_json that is not a JSON object.")
        tests.append(TestCase(name=item["name"], method=item["method"], path=item["path"],
                              expected_status=item["expected_status"], kind=item["kind"],
                              query=query, headers=headers, payload=payload))
    return tests


def render(tests: list[TestCase], base_url: str) -> str:
    lines = ["\"\"\"Generated by openapi_to_tests.py. Do not edit generated output.\"\"\"", "import os", "import httpx", "import pytest", "", f"BASE_URL = os.getenv(\"API_BASE_URL\", {base_url!r}).rstrip(\"/\")", "", "@pytest.fixture", "def client():", "    with httpx.Client(base_url=BASE_URL, timeout=10.0) as api_client:", "        yield api_client", ""]
    for test in tests:
        lines += [f"def {safe_name(test.name)}(client):", f"    response = client.request({test.method!r}, {test.path!r}, params={test.query!r}, headers={test.headers!r}, json={test.payload!r})", f"    assert response.status_code == {test.expected_status}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pytest contract tests from OpenAPI.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("generated_tests/test_generated_api.py"))
    parser.add_argument("--plan-out", type=Path, default=None, help="Where to write the JSON test plan.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--llm", action="store_true", help="Use Groq to make the test plan; requires GROQ_API_KEY.")
    parser.add_argument("--model", default=GROQ_MODEL)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    spec = load_spec(args.spec)
    base_url = args.base_url or os.getenv("API_BASE_URL") or spec.get("servers", [{}])[0].get("url", API_BASE_URL)
    all_tests: list[TestCase] = []
    key = GROQ_API_KEY
    for operation in normalized_operations(spec):
        LOGGER.info("Processing %s %s (%s)", operation["method"], operation["path"], operation["operation_id"])
        if args.llm:
            if not key:
                raise SystemExit("--llm requires the GROQ_API_KEY environment variable.")
            all_tests.extend(groq_plan(operation, key, args.model))
        else:
            planned = offline_plan(spec, operation)
            LOGGER.info("Deterministic test plan for %s:\n%s", operation["operation_id"], json.dumps([test_to_dict(test) for test in planned], indent=2))
            all_tests.extend(planned)
    if not all_tests:
        raise SystemExit("No runnable tests could be derived from this specification.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(all_tests, base_url), encoding="utf-8")
    plan_out = args.plan_out or args.out.with_suffix(".plan.json")
    plan_out.write_text(json.dumps([test_to_dict(test) for test in all_tests], indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(all_tests)} runnable pytest tests: {args.out}")
    print(f"Saved test plan: {plan_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
