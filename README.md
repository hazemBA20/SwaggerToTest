# SwaggerToTest

Phase 1 demo: turn an OpenAPI YAML/JSON document into a runnable `pytest` API
contract-test file. The generator uses a language-neutral test plan and a
deterministic Python renderer.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Start the demo API

In a separate terminal, start the included FastAPI service. It exposes a CRUD
team-directory API: list/filter/search users, create, read, partially update,
and delete. It includes query parameters, optional correlation headers,
pagination, request validation, reusable schemas, and 400/404/409 responses.

```powershell
uvicorn demo_api:app --reload --port 8000
```

Visit `http://localhost:8000/docs` to inspect the live Swagger UI.

The demo also has a hidden `POST /__test/reset` endpoint, used only to reset
its in-memory data before each generated test. `.env.example` enables it with
`TEST_RESET_PATH=/__test/reset`. Do **not** configure a reset path for a real
production API; use a disposable test environment and its normal setup flow.

## Generate tests

The offline mode is deterministic and does not need an LLM key:

```powershell
python openapi_to_tests.py --spec sample_openapi.yaml --out generated_tests/test_api.py
```

To ask Groq to propose the conservative test plan first:

```powershell
$env:GROQ_API_KEY = "gsk_..."
python openapi_to_tests.py --spec sample_openapi.yaml --out generated_tests/test_api.py --llm
```

Run the generated file against the API documented in `servers[0].url`, or
override it:

```powershell
$env:API_BASE_URL = "http://localhost:8000"
pytest generated_tests/test_api.py
```

The output file is runnable Python and uses `httpx`. It currently supports
local `$ref` values, JSON request bodies, required path/query parameters, and
conservative positive/required-field-negative tests. The LLM is optional; it
returns structured plans rather than executable code. Every run logs each
operation and its plan, and saves the complete plan alongside generated code,
for example `generated_tests/test_api.plan.json`.

## Groq troubleshooting

`HTTP 403 / error 1010` means Groq's network edge rejected the request before
the model was invoked. It is different from a bad API key (`401`) or a model
permission restriction (`403` with a JSON Groq error). Try another network,
disable a restrictive proxy/VPN, and contact Groq support with the printed
Cloudflare Ray ID if it persists. Do not put your API key in a support request.
