from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class ResolvedOperation(BaseModel):
    operation_id: str
    method: str
    path: str
    summary: str = ""
    media_type: str | None = None
    request_schema: dict | None = None
    response_schemas: dict[int, dict] = Field(default_factory=dict)
    documented_statuses: list[int] = Field(default_factory=list)
    query_parameters: list[dict] = Field(default_factory=list)
    path_parameters: list[dict] = Field(default_factory=list)
    header_parameters: list[dict] = Field(default_factory=list)
    security_headers_required: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    operation_id: str
    name: str
    kind: Literal["positive", "negative"]
    method: str
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    payload_json: dict | None = None
    payload_form: dict[str, Any] | None = None
    expected_status: int
    revision: int = 0


class FlowStep(BaseModel):
    operation_id: str
    method: str
    path: str
    expected_status: int
    payload_json: dict | None = None
    extract: dict[str, str] = Field(default_factory=dict)


class TestFlow(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    steps: list[FlowStep]
    reset_path: str = ""


class ValidationError(BaseModel):
    test_id: str
    reason: str


class ExecutedTestResult(BaseModel):
    test_id: str
    passed: bool
    status_code: int | None = None
    traceback: str | None = None


class TriageResult(BaseModel):
    test_id: str
    category: Literal["environment", "generation_bug", "real_bug", "passed"]
    detail: str


class Patch(BaseModel):
    test_id: str
    field: str
    new_value: Any


class TestCasePlan(BaseModel):
    tests: list[TestCase]


class PatchPlan(BaseModel):
    patches: list[Patch]


class TriageDecision(BaseModel):
    category: Literal["environment", "generation_bug", "real_bug"]
    detail: str


class PipelineState(BaseModel):
    spec_path: str
    base_url: str = "http://localhost:8000"
    reset_path: str | None = None
    model: str = "claude-sonnet-4-20250514"
    report_path: str = ""
    operations: list[ResolvedOperation] = Field(default_factory=list)
    auth_config: dict[str, str | None] = Field(default_factory=dict)
    plan: list[TestCase] = Field(default_factory=list)
    flows: list[TestFlow] = Field(default_factory=list)
    validation_errors: list[ValidationError] = Field(default_factory=list)
    plan_retries: dict[str, int] = Field(default_factory=dict)
    pending_plan_operations: list[str] = Field(default_factory=list)
    code_path: str = ""
    exec_results: list[ExecutedTestResult] = Field(default_factory=list)
    triage: list[TriageResult] = Field(default_factory=list)
    findings: list[TriageResult] = Field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 3
    dropped_test_ids: list[str] = Field(default_factory=list)


class DependencyEdge(BaseModel):
    producer_operation_id: str
    consumer_operation_id: str
    response_field: str
    path_parameter: str
