from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from openapi_core import OpenAPI

from pipeline.models import PipelineState, ResolvedOperation

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def materialize(value: Any) -> Any:
    """Convert jsonschema_path SchemaPath nodes into plain Python structures."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [materialize(item) for item in value]
    if hasattr(value, "keys"):
        try:
            return {key: materialize(value[key]) for key in value.keys()}
        except (KeyError, TypeError):
            return str(value)
    return value


def header_from_security_scheme(scheme: dict[str, Any]) -> str:
    scheme_type = scheme.get("type")
    if scheme_type == "http" and scheme.get("scheme") == "bearer":
        return "Authorization"
    if scheme_type == "apiKey" and scheme.get("in") == "header":
        name = scheme.get("name")
        if not name:
            raise ValueError("apiKey security scheme missing header name")
        return str(name)
    raise ValueError(
        f"Unsupported security scheme: type={scheme_type!r}, "
        "only http bearer and apiKey in header are supported"
    )


def collect_security_headers(spec_root: Any, operation: Any) -> list[str]:
    security_schemes = materialize(spec_root.get("components", {}).get("securitySchemes", {}))
    if not security_schemes:
        return []

    requirements = materialize(operation.get("security"))
    if requirements is None:
        requirements = materialize(spec_root.get("security", []))

    headers: list[str] = []
    seen: set[str] = set()
    for requirement in requirements or []:
        if not isinstance(requirement, dict):
            continue
        for scheme_name in requirement:
            scheme = security_schemes.get(scheme_name)
            if not scheme:
                continue
            header = header_from_security_scheme(scheme)
            if header not in seen:
                seen.add(header)
                headers.append(header)
    return headers


def auth_env_var(header: str) -> str:
    if header == "Authorization":
        return "API_BEARER_TOKEN"
    return f"API_KEY_{header}"


def build_auth_config(required_headers: list[str]) -> dict[str, str | None]:
    config: dict[str, str | None] = {}
    for header in required_headers:
        value = os.getenv(auth_env_var(header))
        config[header] = value if value else None
    return config


def merge_parameters(path_item: Any, operation: Any) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    for source in (path_item.get("parameters", []), operation.get("parameters", [])):
        for param in source or []:
            params.append(materialize(param))
    return params


def split_parameters(parameters: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    query: list[dict] = []
    path: list[dict] = []
    header: list[dict] = []
    for param in parameters:
        location = param.get("in")
        entry = {
            "name": param.get("name", ""),
            "required": bool(param.get("required")),
            "schema": materialize(param.get("schema", {})),
        }
        if location == "query":
            query.append(entry)
        elif location == "path":
            path.append(entry)
        elif location == "header":
            header.append(entry)
    return query, path, header


def response_schemas_for_operation(operation: Any) -> tuple[dict[int, dict], list[int]]:
    schemas: dict[int, dict] = {}
    statuses: list[int] = []
    for code, response in (operation.get("responses") or {}).items():
        if not str(code).isdigit():
            continue
        status = int(code)
        statuses.append(status)
        content = materialize(response.get("content", {}))
        json_schema = content.get("application/json", {}).get("schema")
        if json_schema:
            schemas[status] = materialize(json_schema)
    statuses.sort()
    return schemas, statuses


def resolve_operation(spec_root: Any, path: str, method: str, operation: Any) -> ResolvedOperation:
    path_item = spec_root["paths"][path]
    parameters = merge_parameters(path_item, operation)
    query_parameters, path_parameters, header_parameters = split_parameters(parameters)

    request_body = operation.get("requestBody")
    media_type: str | None = None
    request_schema: dict | None = None
    if request_body:
        content = materialize(request_body.get("content", {}))
        if content:
            media_type = next(iter(content.keys()))
            request_schema = materialize(content[media_type].get("schema"))

    response_schemas, documented_statuses = response_schemas_for_operation(operation)
    security_headers = collect_security_headers(spec_root, operation)

    return ResolvedOperation(
        operation_id=operation.get("operationId") or f"{method}_{path}",
        method=method.upper(),
        path=path,
        summary=operation.get("summary") or "",
        media_type=media_type,
        request_schema=request_schema,
        response_schemas=response_schemas,
        documented_statuses=documented_statuses,
        query_parameters=query_parameters,
        path_parameters=path_parameters,
        header_parameters=header_parameters,
        security_headers_required=security_headers,
    )


def load_operations(spec_path: str) -> tuple[list[ResolvedOperation], dict[str, str | None]]:
    path = Path(spec_path)
    app = OpenAPI.from_file_path(str(path))
    spec_root = app.spec

    operations: list[ResolvedOperation] = []
    all_security_headers: list[str] = []

    for path in spec_root.get("paths", {}):
        path_item = spec_root["paths"][path]
        for method in path_item.keys():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = materialize(path_item[method])
            resolved = resolve_operation(spec_root, path, method, operation)
            operations.append(resolved)
            for header in resolved.security_headers_required:
                if header not in all_security_headers:
                    all_security_headers.append(header)

    auth_config = build_auth_config(all_security_headers)
    return operations, auth_config


def load_spec_node(state: PipelineState) -> dict[str, Any]:
    operations, auth_config = load_operations(state.spec_path)
    return {
        "operations": operations,
        "auth_config": auth_config,
        "pending_plan_operations": [op.operation_id for op in operations],
    }
