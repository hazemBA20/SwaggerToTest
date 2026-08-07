"""Generated contract tests — do not edit manually."""

import os
from pathlib import Path

import httpx
import jsonschema
import pytest

BASE_URL = os.getenv(
    "API_BASE_URL", "https://test.ai.avidea.tn/api/avidea-mind-utils/v1"
).rstrip("/")
TEST_RESET_PATH = os.getenv("TEST_RESET_PATH", "")
TEST_DATA_DIR = os.getenv("TEST_DATA_DIR", "carte_grise")

AUTH_HEADERS = {}


def merge_headers(test_headers: dict) -> dict:
    merged = dict(AUTH_HEADERS)
    merged.update(test_headers or {})
    return merged


def resolve_bindings(value, bindings):
    if isinstance(value, str):
        return value.format_map(bindings)
    if isinstance(value, list):
        return [resolve_bindings(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: resolve_bindings(item, bindings) for key, item in value.items()}
    return value


def split_multipart(payload_form: dict) -> tuple[dict, dict]:
    files = {}
    data = {}
    for key, value in (payload_form or {}).items():
        if isinstance(value, dict) and value.get("__file__"):
            filename = value.get("filename", "test.bin")
            content_type = value.get("content_type", "application/octet-stream")
            # Try to find a real file in TEST_DATA_DIR
            test_data_path = Path(TEST_DATA_DIR)
            if test_data_path.exists():
                # Look for any image file in the test data directory
                image_files = (
                    list(test_data_path.rglob("*.jpg"))
                    + list(test_data_path.rglob("*.png"))
                    + list(test_data_path.rglob("*.jpeg"))
                )
                if image_files:
                    # Use the first available image file
                    real_file_path = image_files[0]
                    with open(real_file_path, "rb") as f:
                        file_content = f.read()
                    files[key] = (filename, file_content, content_type)
                else:
                    # Fallback to placeholder if no files found
                    files[key] = (filename, b"test-content", content_type)
            else:
                # Fallback to placeholder if directory doesn't exist
                files[key] = (filename, b"test-content", content_type)
        else:
            data[key] = value
    return files, data


def assert_response_schema(response_json, schema):
    jsonschema.Draft202012Validator(schema).validate(response_json)


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as api_client:
        if TEST_RESET_PATH:
            reset_response = api_client.post(TEST_RESET_PATH)
            assert (
                reset_response.status_code == 204
            ), f"Could not reset test data: {reset_response.status_code}"
        yield api_client


def test_getversion_get_api_version_successfully(client):
    """# test_id: c58039d8-6f1f-4f73-afc3-f1e278e11d4f"""
    headers = merge_headers({})
    response = client.request(
        "GET",
        "/version",
        params={},
        headers=headers,
    )
    print(f"STATUS:{response.status_code}")
    assert response.status_code == 200
    assert_response_schema(
        response.json(),
        {
            "description": "VersionResponse details all API version information.",
            "properties": {
                "version": {
                    "description": "version string",
                    "example": "1.0-20260424",
                    "type": "string",
                }
            },
            "required": {"0": "version"},
            "type": "object",
        },
    )


def test_extractregistrationcard_negative_test_missing_required_fields(client):
    """# test_id: e84ce8b3-625a-49b4-8aab-c496dee3e584"""
    headers = merge_headers({})
    _files, _data = split_multipart(
        {
            "registrationDocument": "__file__",
            "requestId": "892d5757-28cc-407f-881c-55833811599d",
        }
    )
    response = client.request(
        "POST",
        "/registration-card/extract",
        params={},
        headers=headers,
        files=_files or None,
        data=_data or None,
    )
    print(f"STATUS:{response.status_code}")
    assert response.status_code == 400
    assert_response_schema(
        response.json(),
        {
            "description": "Message format when any error occurs.",
            "properties": {
                "api-version": {
                    "description": "Server side api-version",
                    "example": "1.0",
                    "type": "string",
                },
                "details": {
                    "description": "A user friendly error message. Can be null or non present",
                    "example": "mission not found",
                    "type": "string",
                },
                "error": {
                    "description": "An error code",
                    "example": "NOT_FOUND",
                    "type": "string",
                },
                "success": {
                    "description": "The value must be \u0027false\u0027",
                    "enum": {"0": false},
                    "example": false,
                    "type": "boolean",
                },
            },
            "required": {"0": "success", "1": "error"},
            "type": "object",
        },
    )
