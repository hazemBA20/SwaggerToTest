# Agentic API Contract Test Generator — Technical Specification

## 1. Purpose

Replace a one-shot OpenAPI-to-pytest generator with a **LangGraph agent loop**: plan tests →
validate → render → execute against a real API → triage failures → revise → repeat, bounded by
an iteration cap. The loop must distinguish three failure categories and act differently on each:

- **environment** — harness misconfiguration (e.g. missing auth), not a code problem
- **generation_bug** — the generated test itself is wrong (bad payload shape, wrong media type)
- **real_bug** — the API violates its own OpenAPI contract — this is a *finding*, never "fixed"
  by weakening the test

This is the single most important behavioral requirement in this spec: **the revise node may
only touch tests triaged as `generation_bug`.** `real_bug` triage results go straight to the
final report, untouched.

There is no offline heuristic fallback in this version — do not implement anything resembling
the old `offline_plan`. Grounding against the spec is the job of the deterministic **validate**
node, not a second parallel generator.

## 2. Architecture

Six-stage LangGraph `StateGraph` with one feedback loop:

```
load_spec → plan → validate → render → execute → triage → report (END)
              ^                  |         |         |
              |                  |         |         └─(generation_bug present)─→ revise ─┐
              └───(invalid, retries left)───┘                                              |
              ^                                                                            |
              └────────────────────────────(revise output)────────────────────────────────┘
```

- `plan` and `revise` are the only LLM nodes.
- `load_spec`, `validate`, `render`, `execute`, `triage` are pure deterministic code — no LLM
  calls, no non-determinism.
- `revise` output re-enters at `validate`, not `render` — a patch must pass the same guardrail
  a fresh plan does.
- Two independent retry counters:
  - `plan_retries` (per operation, cap 2): validate → plan loop for ungrounded output
  - `iteration` (global, cap 3, default): plan → …→ triage → revise loop for fixing real
    generation bugs found only after execution

## 3. Dependencies

```
langgraph
langchain-anthropic
openapi-core>=0.19
pydantic>=2
jinja2
jsonschema>=4.18
httpx
pytest
pytest-json-report
black
python-dotenv
```

## 4. Data Models

All models are Pydantic `BaseModel`s unless noted. Use `pydantic.BaseModel` throughout, not
dataclasses — the LLM nodes need `.with_structured_output()` compatibility.

```python
from typing import Literal, Any
from pydantic import BaseModel, Field
import uuid

class ResolvedOperation(BaseModel):
    operation_id: str
    method: str
    path: str
    summary: str = ""
    media_type: str | None            # "application/json", "multipart/form-data", or None (no body)
    request_schema: dict | None        # resolved JSON Schema for the body, keyed by declared media_type
    response_schemas: dict[int, dict]  # status code -> resolved JSON Schema for that response
    documented_statuses: list[int]
    query_parameters: list[dict]       # each: {"name", "required", "schema"}
    path_parameters: list[dict]
    header_parameters: list[dict]
    security_headers_required: list[str]  # e.g. ["Authorization", "X-API-KEY"], from spec's security schemes

class TestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # stable across revisions
    operation_id: str
    name: str
    kind: Literal["positive", "negative"]
    method: str
    path: str
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}       # test-specific headers only — auth is injected at render time
    payload_json: dict | None = None   # used when operation.media_type == "application/json"
    payload_form: dict[str, Any] | None = None  # used when media_type == "multipart/form-data";
                                                 # file fields use {"__file__": true, "filename": str, "content_type": str}
    expected_status: int
    revision: int = 0

class FlowStep(BaseModel):
    operation_id: str
    method: str
    path: str                          # may contain {binding_name} placeholders
    expected_status: int
    payload_json: dict | None = None
    extract: dict[str, str] = {}       # binding_name -> response field name

class TestFlow(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    steps: list[FlowStep]

class ValidationError(BaseModel):
    test_id: str
    reason: str                        # human-readable, fed back to the plan node on retry

class ExecutedTestResult(BaseModel):
    test_id: str
    passed: bool
    status_code: int | None
    traceback: str | None

class TriageResult(BaseModel):
    test_id: str
    category: Literal["environment", "generation_bug", "real_bug", "passed"]
    detail: str

class Patch(BaseModel):
    test_id: str
    field: str                         # which TestCase field to change
    new_value: Any

class PipelineState(BaseModel):
    spec_path: str
    operations: list[ResolvedOperation] = []
    auth_config: dict[str, str] = {}
    plan: list[TestCase] = []
    flows: list[TestFlow] = []
    validation_errors: list[ValidationError] = []
    plan_retries: dict[str, int] = {}  # operation_id -> retry count
    code_path: str = ""
    exec_results: list[ExecutedTestResult] = []
    triage: list[TriageResult] = []
    findings: list[TriageResult] = []  # accumulated real_bug results, carried across iterations
    iteration: int = 0
    max_iterations: int = 3
```

## 5. Node Specifications

### 5.1 `load_spec` (deterministic)

- Load the OpenAPI document with `openapi_core.Spec.from_file_path(...)`.
- For every operation, resolve refs (`openapi-core` handles local and external `$ref`) and build
  a `ResolvedOperation`. Critically: read the request body by iterating `content.keys()` and
  taking whatever media type is present — **do not hardcode `application/json`.**
- Derive `security_headers_required` from the spec's `security` + `components.securitySchemes`:
  - `type: http, scheme: bearer` → header name `Authorization`
  - `type: apiKey, in: header, name: X` → header name `X`
  - Anything else (oauth2, apiKey in query/cookie) → raise a clear "unsupported security scheme"
    error rather than silently skipping it.
- Build `auth_config`: for each required header, read a value from an env var
  (`API_BEARER_TOKEN` for `Authorization`, or `API_KEY_<HEADER_NAME>` for named API keys). If a
  required env var is missing, do not fail the whole pipeline — set `auth_config[header] = None`
  and let `execute`/`triage` surface it as an `environment` failure, since a missing token is
  exactly the kind of thing triage exists to categorize correctly.

### 5.2 `plan` (LLM, structured output)

- One call per `ResolvedOperation` not yet planned this run (or per validation retry, one call
  per operation that failed validation).
- Use `ChatAnthropic(model=...).with_structured_output(list[TestCase])` or a wrapper model
  `class TestCasePlan(BaseModel): tests: list[TestCase]`.
- System prompt must state explicitly:
  - Use only facts present in the given `ResolvedOperation` — no invented fields, paths, or
    statuses.
  - `expected_status` must be one of `documented_statuses`.
  - If `media_type == "multipart/form-data"`, populate `payload_form`, not `payload_json`, and
    mark required file fields with `{"__file__": true, "filename": "test.jpg",
    "content_type": "image/jpeg"}` (or an appropriate type inferred from the field's
    description/format).
  - Do not include auth headers — those are injected downstream.
  - Produce one `positive` test per documented 2xx status, plus `negative` tests only for
    constraints explicitly present in the schema (required fields, enums) — same conservative
    scope as the old `offline_plan`, just LLM-authored and validated afterward instead of
    heuristically generated.
- On a validation retry, append the specific `ValidationError.reason` strings for that
  operation's failed tests to the prompt and ask for a corrected plan for just those tests.

### 5.3 `validate` (deterministic — the guardrail)

For every `TestCase` in `state.plan`, check against its `ResolvedOperation` (matched by
`operation_id`):

1. `method` matches the operation's method.
2. `path` matches the operation's path (after path-parameter substitution).
3. `expected_status` is in `documented_statuses`.
4. If `payload_json` is set: every key is a property in `request_schema`; every `required`
   property in `request_schema` is present unless the test's `kind == "negative"` and is
   specifically testing a missing-field case.
5. If `payload_form` is set: same, against the multipart schema's properties.
6. If the operation's `media_type` is `application/json`, `payload_form` must be `None`, and
   vice versa — a plan that puts a JSON body on a multipart operation is invalid.
7. Enum-valued fields in the payload must use a value from the schema's `enum`, unless the test
   is a negative enum-violation test (name should indicate this).

Also validate every `TestFlow` the same way `validate_flows` did in the prior version: bindings
used before they're extracted, statuses/operations must exist, transitions must be backed by
inferred producer→consumer edges.

Failures become `ValidationError` entries. Route:
- If `plan_retries[operation_id] < 2` → increment retry counter, go back to `plan` for just the
  failed tests on that operation.
- Else → drop those specific tests, log them as unresolved, continue with whatever validated.

### 5.4 `render` (deterministic)

- Jinja2 template, not string concatenation. Output must be passed through `black` before being
  written to disk (call `black.format_str(code, mode=black.Mode())` or shell out to `black`).
- Every request in the rendered file must merge `auth_config` headers into the request headers
  automatically — this happens in the template/render step, never authored by the LLM.
- Multipart tests render as `client.request(method, path, params=query, headers=headers,
  files={...}, data={...})`, splitting `payload_form` into file fields (`files=`) and plain
  fields (`data=`) based on the `__file__` marker. JSON tests render as `json=payload_json`.
- Response assertions use `jsonschema.validate(response.json(), schema)` (via
  `jsonschema.Draft202012Validator`), not a hand-rolled recursive checker.
- Each rendered test function embeds `test.id` as a pytest marker or in a docstring
  (`# test_id: <uuid>`) so `execute`/`triage` can correlate results back to `TestCase` objects
  by ID, independent of function-name changes across revisions.
- Flows render the same way as the prior version's `test_flow_*` functions, with
  `resolve_bindings` kept as-is — that part of the original renderer was fine.

### 5.5 `execute` (tool call)

- Run `pytest <generated_file> --json-report --json-report-file=<report_path> -v` as a
  subprocess.
- Parse `<report_path>` (the `pytest-json-report` schema) into `ExecutedTestResult` entries,
  matching by the embedded `test_id` rather than function name.
- Capture status code from the traceback/response when available (e.g. by having the rendered
  test print `f"STATUS:{response.status_code}"` before asserting, and scraping stdout per test
  from the JSON report's captured output) — needed for triage rule #1 below.

### 5.6 `triage` (deterministic rules, LLM fallback only for ambiguous cases)

Apply in order, per failing test:

1. **environment**: any `auth_config` value used by this test's headers is `None` (missing env
   var), OR the observed status code is 401/403 while the operation's `security_headers_required`
   is non-empty. This must be checked *before* rule 2 — an auth failure should never be
   misclassified as a generation bug.
2. **generation_bug**: the request itself was malformed independent of the response — e.g. a
   multipart operation whose required file field is missing from `payload_form` (a validation
   gap that slipped through), or observed status is a generic 4xx that doesn't match any
   documented negative-test intent.
3. **real_bug**: request was well-formed (passed validation, correct media type, no auth issue)
   but the response status or schema doesn't match what the operation documents. This is a
   contract violation in the API itself.
4. Anything not confidently matching rules 1–3 → one LLM call with the operation, the test, and
   the failure detail, forced to pick one of the three categories plus a one-sentence reason.

Tests that pass get `category: "passed"`.

### 5.7 `revise` (LLM, patch-only)

- Input: for each `generation_bug`-triaged test, the original `TestCase`, its `ResolvedOperation`,
  and the failure detail.
- Output: `list[Patch]` — field-level changes only (e.g. `payload_form` needs the missing file
  field added), not a full new `TestCase`. Apply patches to the existing `TestCase` objects
  (bump `revision`, keep the same `id`), then route back to `validate`.
- `real_bug` and `environment` triage results are never passed to this node.

### 5.8 `report` (terminal)

- Write a summary: counts by triage category, and full detail (operation, test, expected vs.
  actual) for every `real_bug` finding — this is the actual deliverable of a contract-test run,
  distinct from "tests passed."
- Stop conditions, any of which route here: no failing tests remain; `iteration >= max_iterations`;
  or every remaining failure is `real_bug`/`environment` (nothing left for `revise` to act on).

## 6. Graph Definition (LangGraph)

```python
from langgraph.graph import StateGraph, END

graph = StateGraph(PipelineState)
graph.add_node("load_spec", load_spec)
graph.add_node("plan", plan)
graph.add_node("validate", validate)
graph.add_node("render", render)
graph.add_node("execute", execute)
graph.add_node("triage", triage)
graph.add_node("revise", revise)
graph.add_node("report", report)

graph.set_entry_point("load_spec")
graph.add_edge("load_spec", "plan")
graph.add_edge("plan", "validate")
graph.add_conditional_edges("validate", route_after_validate,
    {"retry_plan": "plan", "render": "render"})
graph.add_edge("render", "execute")
graph.add_edge("execute", "triage")
graph.add_conditional_edges("triage", route_after_triage,
    {"revise": "revise", "report": "report"})
graph.add_edge("revise", "validate")
graph.add_edge("report", END)

app = graph.compile(checkpointer=...)  # e.g. MemorySaver, for resumability / human approval before execute
```

`route_after_validate`: has any operation hit its retry cap with unresolved errors this pass? →
`render` anyway (with those tests dropped). Otherwise, any pending retries → `plan`. Else →
`render`.

`route_after_triage`: any `generation_bug` results AND `state.iteration < state.max_iterations` →
increment `iteration`, go to `revise`. Else → `report`.

## 7. CLI

```
python -m pipeline.cli \
  --spec path/to/openapi.yaml \
  --base-url https://api.example.com \
  --out generated_tests/test_generated.py \
  --report-out generated_tests/report.md \
  --max-iterations 3 \
  --model claude-sonnet-4-6
```

Required env vars: `ANTHROPIC_API_KEY`, plus whatever `API_BEARER_TOKEN` / `API_KEY_<NAME>` the
target spec's security schemes call for (missing ones are allowed — they'll surface as
`environment` triage results, not a hard crash).

## 8. Project Layout

```
pipeline/
  models.py        # section 4
  spec_loader.py    # 5.1
  planner.py        # 5.2
  validator.py       # 5.3
  renderer.py         # 5.4 + templates/test_template.py.jinja2
  executor.py          # 5.5
  triage.py            # 5.6
  reviser.py            # 5.7
  reporter.py            # 5.8
  graph.py                # section 6
  cli.py                   # section 7
templates/
  test_template.py.jinja2
requirements.txt
```

## 9. Acceptance Scenarios

Use these as concrete pass/fail checks for the implementation — both fixtures are real specs
already used to validate this design.

**Fixture A — `avidea-mind-openapi.yaml`** (two operations: `getVersion` with no body,
`extractRegistrationCard` with a multipart file upload, both requiring bearer + API-key auth):
- `load_spec` must resolve `extractRegistrationCard`'s request schema even though it's
  `multipart/form-data`, not `application/json`.
- Without `API_BEARER_TOKEN`/`API_KEY_X-AVIDEA-MIND-...` set, every generated test's execution
  failure must triage as `environment`, never `generation_bug` or `real_bug`.
- With auth configured but no file supplied, a missing-file plan must be caught by `validate`
  (rule 5) before ever reaching `render` — it should not need a failed execution + revise cycle
  to be caught.
- No `TestFlow`s should be generated — there's no id-returning POST with a matching `/{id}`
  child route in this spec, and the pipeline must not fabricate one.

**Fixture B — a small CRUD spec** (`POST /users` returning `id`, `GET/PATCH/DELETE
/users/{id}`): exercises the full loop — flow inference, execution against a mock server,
and, if you seed the mock server to intentionally violate its own documented response schema on
one field, confirms that failure is triaged `real_bug` and appears in the final report untouched
by `revise`, rather than the test being patched to match the broken response.

## 10. Explicit Non-Goals (v1)

- No reintroduction of a heuristic/offline planning path — `validate` is the only grounding
  mechanism.
- No Schemathesis/Hypothesis-based fuzzing integration — plan-generated tests only.
- No OAuth2 flows beyond static bearer tokens read from env vars.
- No Karate/other-framework output — pytest only.
- Dependency-graph inference stays the same POST-returns-id + child-path heuristic as the prior
  version; OpenAPI `links`-based inference is a future improvement, not required here.
