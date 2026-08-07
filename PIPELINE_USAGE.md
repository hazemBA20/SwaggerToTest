# Agentic API Contract Test Pipeline - Usage Guide

## Overview

The agentic pipeline is a LangGraph-based agent loop that generates, validates, executes, and iteratively improves API contract tests from OpenAPI specifications. It distinguishes between environment issues, generation bugs, and real API contract violations.

## Setup

### 1. Install Dependencies

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# Required: API base URL for testing
API_BASE_URL=http://localhost:8000

# Required: LLM Provider API Key (choose one)
# For Anthropic (default provider)
ANTHROPIC_API_KEY=your-anthropic-api-key
# For Groq
GROQ_API_KEY=your-groq-api-key
# For Google AI Studio / Gemini
GOOGLE_API_KEY=your-google-api-key

# Optional: API authentication based on your OpenAPI spec's security schemes
# For Bearer token auth (type: http, scheme: bearer)
API_BEARER_TOKEN=your-bearer-token

# For API key auth in headers (type: apiKey, in: header, name: X-HEADER-NAME)
API_KEY_X_API_KEY=your-api-key

# Optional: Reset endpoint for test isolation (demo/test environments only)
TEST_RESET_PATH=/__test/reset
```

### 3. Start Your API

Ensure your target API is running and accessible at `API_BASE_URL`.

## CLI Usage

### Basic Command

```powershell
python -m pipeline.cli \
  --spec path/to/openapi.yaml \
  --base-url https://api.example.com \
  --out generated_tests/test_generated.py \
  --report-out generated_tests/report.md
```

### All Arguments

- `--spec` (required): Path to OpenAPI YAML/JSON specification
- `--base-url` (required): API base URL for generated tests
- `--out` (required): Output path for generated pytest module
- `--report-out` (required): Output path for markdown report
- `--reset-path` (optional): Reset endpoint path for test isolation (e.g., `/__test/reset`)
- `--provider` (optional, default: `anthropic`): LLM provider (`anthropic`, `groq`, or `google`)
- `--model` (optional): Model name (defaults vary by provider)
- `--max-iterations` (optional, default: 3): Maximum revision iterations for fixing generation bugs

### With Reset Path (for Test Environments)

```powershell
python -m pipeline.cli \
  --spec avidea-mind-openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --reset-path /__test/reset
```

### Custom Iteration Limit

```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --max-iterations 5
```

### Using Groq Provider

```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --provider groq
```

Default Groq model: `llama-3.3-70b-versatile`

### Using Google AI Studio / Gemini Provider

```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --provider google
```

Default Google model: `gemini-3.1-flash-lite`

### Custom Model (Any Provider)

```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --provider groq \
  --model llama-3.1-70b-versatile
```

## Pipeline Behavior

### The Agent Loop

The pipeline follows this flow:

```
load_spec → plan → validate → render → execute → triage → report (END)
              ^                  |         |         |
              |                  |         |         └─(generation_bug)─→ revise ─┐
              └───(invalid, retries left)───┘                              |
              ^                                                            |
              └────────────────────(revise output)─────────────────────────┘
```

### Triage Categories

The pipeline classifies test failures into three categories:

1. **environment**: Harness misconfiguration (missing auth, 401/403 when auth required)
   - Not a code problem
   - Check your `API_BEARER_TOKEN` or `API_KEY_*` environment variables

2. **generation_bug**: The generated test is wrong (bad payload, wrong media type)
   - The pipeline will automatically revise and retry (up to `max-iterations`)
   - Revision only touches fields, never weakens assertions

3. **real_bug**: API violates its own OpenAPI contract
   - This is a finding - the API is broken, not the test
   - Appears in the final report with full details
   - Never automatically fixed by the pipeline

### Retry Logic

- **Plan retries**: Up to 2 retries per operation if validation fails
- **Iteration retries**: Up to `max-iterations` (default 3) global revisions for generation bugs

## Running Generated Tests

After the pipeline completes, run the generated tests:

```powershell
pytest generated_tests/test_generated.py -v
```

The generated tests use `httpx` and require the API to be running at the configured base URL.

## Understanding the Report

The markdown report contains:

- **Summary**: Counts by triage category (passed, environment, generation_bug, real_bug)
- **Contract violations (real_bug)**: Detailed findings for each API contract violation
- **Dropped tests**: Tests that couldn't be validated after retry caps

Example report section:

```markdown
## Contract violations (real_bug)

### getVersion — test `getVersion_success_200`
- **Category:** real_bug
- **Detail:** Expected status 200, got 500
- **Expected status:** 200
```

## Authentication Setup

### Bearer Token Authentication

If your OpenAPI spec uses:
```yaml
security:
  - bearerAuth: []
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
```

Set in `.env`:
```bash
API_BEARER_TOKEN=your-token-here
```

### API Key Authentication

If your OpenAPI spec uses:
```yaml
security:
  - apiKey: []
components:
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: X-API-KEY
```

Set in `.env`:
```bash
API_KEY_X_API_KEY=your-api-key-here
```

The header name is converted to uppercase with underscores (e.g., `X-API-KEY` → `API_KEY_X_API_KEY`).

### Multiple Security Schemes

If your spec requires multiple authentication methods, set all required environment variables. Missing credentials will be triaged as `environment` failures.

## Test Isolation with Reset Path

For test environments that support data reset (like the included demo_api), use `--reset-path`:

```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_api.py \
  --report-out generated_tests/report.md \
  --reset-path /__test/reset
```

This enables:
- Automatic data reset before each test flow
- Isolated test execution
- CRUD lifecycle testing (create → read → update → delete)

**Important**: Never use a reset path for production APIs. Use disposable test environments instead.

## Example: Testing the Demo API

1. Start the demo API:
```powershell
uvicorn demo_api:app --reload --port 8000
```

2. Run the pipeline:
```powershell
python -m pipeline.cli \
  --spec sample_openapi.yaml \
  --base-url http://localhost:8000 \
  --out generated_tests/test_demo.py \
  --report-out generated_tests/report_demo.md \
  --reset-path /__test/reset
```

3. Run generated tests:
```powershell
pytest generated_tests/test_demo.py -v
```

## Troubleshooting

### "Required auth credential missing from environment"

- Check that `API_BEARER_TOKEN` or `API_KEY_*` variables are set in `.env`
- Verify the variable name matches your OpenAPI spec's security scheme

### "Received 401/403 while auth is required by the spec"

- Your API is rejecting the provided credentials
- Verify the token/key is valid and has sufficient permissions

### Generation bugs persist after max_iterations

- The LLM may be unable to fix certain issues
- Review the triage details in the report
- Consider manually adjusting the OpenAPI spec or test expectations

### "No pytest result correlated to test_id"

- The test execution failed to produce a JSON report
- Check that `pytest-json-report` is installed
- Verify the generated test file is syntactically valid

## Exit Codes

- `0`: Success (no contract violations found)
- `2`: Contract violations found (real_bug triage results present)

## Advanced: Custom Model Configuration

### Default Models by Provider

- **Anthropic**: `claude-sonnet-4-6`
- **Groq**: `llama-3.3-70b-versatile`
- **Google**: `gemini-3.1-flash-lite`

### Using Custom Models

To use a specific model:

```powershell
# Anthropic
python -m pipeline.cli \
  --spec openapi.yaml \
  --base-url https://api.example.com \
  --out tests.py \
  --report-out report.md \
  --provider anthropic \
  --model claude-opus-4-20250514

# Groq
python -m pipeline.cli \
  --spec openapi.yaml \
  --base-url https://api.example.com \
  --out tests.py \
  --report-out report.md \
  --provider groq \
  --model llama-3.1-70b-versatile

# Google
python -m pipeline.cli \
  --spec openapi.yaml \
  --base-url https://api.example.com \
  --out tests.py \
  --report-out report.md \
  --provider google \
  --model gemini-1.5-pro
```

Available models depend on your API provider access and current model availability.
