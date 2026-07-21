# SwaggerToTest

Phase 1 demo: turn an OpenAPI YAML/JSON document into a runnable `pytest` API
contract-test file. The generator uses a language-neutral test plan and a
deterministic Python renderer.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

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
returns structured plans rather than executable code.
