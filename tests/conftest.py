import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as gateway

AUTH = {"Authorization": "Bearer test-key"}

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")

CATALOG = {
    "data": [
        {"id": "google/gemini-3.1-flash-image", "created": 1700000000},
        {"id": "openai/gpt-image-2", "created": 1700000001},
        {"id": "bytedance-seed/seedream-5-0-flash", "created": 1700000002},
    ]
}

GENERATION_RESPONSE = {
    "created": 1700000100,
    "data": [{"b64_json": PNG_B64, "media_type": "image/png"}],
    "usage": {"total_tokens": 123, "cost": 0.01},
}


def default_upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/images/models":
        return httpx.Response(200, json=CATALOG)
    return httpx.Response(200, json=GENERATION_RESPONSE)


@pytest.fixture(autouse=True)
def reset_state():
    gateway.catalog.reset()
    gateway.image_store = gateway.ImageStore()
    gateway.app.dependency_overrides.clear()
    yield
    gateway.app.dependency_overrides.clear()


@pytest.fixture
def make_client():
    """Returns a factory (upstream_handler) -> (TestClient, sent_requests)."""

    def _make(handler):
        sent: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return handler(request)

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
        gateway.app.dependency_overrides[gateway.get_upstream_client] = lambda: async_client
        return TestClient(gateway.app), sent

    return _make


@pytest.fixture
def client(make_client):
    tc, sent = make_client(default_upstream)
    tc.sent = sent
    return tc


def sent_payload(sent, index=-1) -> dict:
    return json.loads(sent[index].content)