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
    """# test_id: 3d9a10c5-7398-4ae1-9b23-b9d48df3f826"""
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
            "required": ["version"],
            "type": "object",
        },
    )


def test_extractregistrationcard_extract_registration_card_successfully(client):
    """# test_id: 68ace2dc-3211-47a7-8dbd-8117c5613f90"""
    headers = merge_headers({})
    _files, _data = split_multipart(
        {
            "registrationDocument": {
                "__file__": True,
                "filename": "test.jpg",
                "content_type": "image/jpeg",
            },
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
    assert response.status_code == 200


def test_extractregistrationcard_negative_test_missing_required_fields(client):
    """# test_id: 68ace2dc-3211-47a7-8dbd-8117c5613f91"""
    headers = merge_headers({})
    _files, _data = split_multipart({"requestId": "892d5757-28cc-407f-881c-55833811599d"})
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
