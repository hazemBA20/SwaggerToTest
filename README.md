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

The offline mode is deterministic and does not need an LLM key. Generate
pytest, Karate, or both from the same plan:

```powershell
python openapi_to_tests.py --spec sample_openapi.yaml --out generated_tests/test_api.py
python openapi_to_tests.py --spec sample_openapi.yaml --target karate
python openapi_to_tests.py --spec sample_openapi.yaml --target both --out generated_tests/pytest/test_api.py
```

Generate deterministic CRUD integration flows with `--integration`:

```powershell
python openapi_to_tests.py --spec sample_openapi.yaml --target both --out generated_tests/test_api.py --integration
```

To generate and run only the integration pipeline, use separate default files:

```powershell
python openapi_to_tests.py --spec sample_openapi.yaml --target both --integration-only
pytest generated_tests/test_integration.py
karate run generated_tests/karate/integration.feature
```

This mode builds a dependency graph from documented `POST` response `id`
fields and matching member-path parameters such as `{user_id}`. For the
included API, it generates a create → get → patch → delete lifecycle and a
create → delete → get (`404`) flow. Each flow uses `TEST_RESET_PATH` once at
its start for isolation. The run also writes adjacent `.graph.json` and
`.flows.json` artifacts so the inferred edges and flow plan are auditable.
Integration generation fails when no reset path is configured; use a
disposable API environment with an equivalent reset mechanism.
The optional `--llm` mode still adds only validated single-operation cases;
the demo dependency graph and integration flows remain deterministic.

Use an LLM to propose additional test cases. Choose the provider in `.env`:

```powershell
# Groq
# LLM_PROVIDER=groq
# GROQ_API_KEY=gsk_...

# Google AI Studio / Gemini
# LLM_PROVIDER=google
# GOOGLE_API_KEY=AIza...
# GOOGLE_MODEL=gemini-2.5-flash

python openapi_to_tests.py --spec sample_openapi.yaml --out generated_tests/test_api.py --llm
```

`gemini-2.5-flash` is the Google default. You can temporarily override either
choice with `--provider google --model gemini-2.5-flash`.

Run the generated file against the API documented in `servers[0].url`, or
override it:

```powershell
$env:API_BASE_URL = "http://localhost:8000"
pytest generated_tests/test_api.py
```

## Run Karate output

Install the Karate CLI (and Java 21+ if using the standalone JAR), then run:

```powershell
# The generated feature defaults to http://localhost:8000, so no override is needed.
karate run generated_tests/karate
```

When using the standalone JAR and overriding the URL, pass the Java property
before `-jar`:

```powershell
java -Dapi.baseUrl=http://localhost:8000 -jar karate.jar generated_tests/karate
```

The Karate renderer generates `.feature` scenarios from the same shared plan
as pytest, including request setup, parameters, headers, JSON bodies, status
checks, reset isolation, and basic response-shape checks.

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
