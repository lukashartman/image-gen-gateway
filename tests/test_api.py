"""Route-level tests against a mocked upstream (no network)."""

from urllib.parse import urlparse

import httpx

import app as gateway
from conftest import AUTH, GENERATION_RESPONSE, PNG, PNG_B64, sent_payload

OPEN_WEBUI_GENERATION_BODY = {
    "model": "dall-e-3",
    "prompt": "a cat in a hat",
    "n": 1,
    "size": "auto",
    "response_format": "b64_json",
}


def gen_body(**overrides):
    return {**OPEN_WEBUI_GENERATION_BODY, **overrides}


def test_missing_auth_rejected(client):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_empty_key_rejected(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer  "})
    assert r.status_code == 401


def test_gateway_mode_wrong_key_rejected(client, monkeypatch):
    monkeypatch.setattr(gateway.settings, "gateway_api_key", "gate-key")
    monkeypatch.setattr(gateway.settings, "openrouter_api_key", "or-key")
    r = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_gateway_mode_uses_openrouter_key(client, monkeypatch):
    monkeypatch.setattr(gateway.settings, "gateway_api_key", "gate-key")
    monkeypatch.setattr(gateway.settings, "openrouter_api_key", "or-key")
    r = client.post(
        "/v1/images/generations",
        headers={**AUTH, "Authorization": "Bearer gate-key"},
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 200
    assert client.sent[-1].headers["authorization"] == "Bearer or-key"


def test_gateway_mode_without_openrouter_key_fails(client, monkeypatch):
    monkeypatch.setattr(gateway.settings, "gateway_api_key", "gate-key")
    monkeypatch.setattr(gateway.settings, "openrouter_api_key", "")
    r = client.post(
        "/v1/images/generations",
        headers={**AUTH, "Authorization": "Bearer gate-key"},
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 500


def test_open_webui_generation_shape_b64(client, monkeypatch):
    monkeypatch.setattr(
        gateway.settings,
        "model_aliases",
        {"dall-e-3": "google/gemini-3.1-flash-image"},
    )
    r = client.post("/v1/images/generations", headers=AUTH, json=gen_body())
    assert r.status_code == 200
    payload = sent_payload(client.sent)
    assert payload["model"] == "google/gemini-3.1-flash-image"
    assert payload["prompt"] == "a cat in a hat"
    assert "size" not in payload  # "auto" dropped
    assert "response_format" not in payload  # never forwarded
    body = r.json()
    assert body["created"] == GENERATION_RESPONSE["created"]
    assert body["data"][0]["b64_json"] == PNG_B64
    assert body["data"][0]["media_type"] == "image/png"
    assert "usage" not in body  # logged, not returned
    assert "url" not in body["data"][0]


def test_url_mode_when_response_format_absent(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="google/gemini-3.1-flash-image", response_format=None),
    )
    assert r.status_code == 200
    url = r.json()["data"][0]["url"]
    assert "/v1/images/" in url
    path = urlparse(url).path
    img = client.get(path, headers=AUTH)
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/png")
    assert img.content == PNG

    assert client.get(path).status_code == 401  # image route is authed too


def test_size_quality_and_conflicting_extras(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(
            model="google/gemini-3.1-flash-image",
            size="1920x1080",
            quality="hd",
            resolution="2K",
            aspect_ratio="16:9",
            seed=5,
        ),
    )
    assert r.status_code == 200
    payload = sent_payload(client.sent)
    assert payload["size"] == "1920x1080"
    assert payload["quality"] == "high"
    assert "resolution" not in payload
    assert "aspect_ratio" not in payload
    assert payload["seed"] == 5


def test_n_clamped(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="openai/gpt-image-2", n=99),
    )
    assert r.status_code == 200
    assert sent_payload(client.sent)["n"] == 10


def test_unknown_model_rejected_naming_catalog(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="not/a-real-model"),
    )
    assert r.status_code == 400
    assert "/v1/models" in r.json()["error"]["message"]


def test_default_model_used_when_absent(client, monkeypatch):
    monkeypatch.setattr(gateway.settings, "default_model", "openai/gpt-image-2")
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json={"prompt": "p", "n": 1, "response_format": "b64_json"},
    )
    assert r.status_code == 200
    assert sent_payload(client.sent)["model"] == "openai/gpt-image-2"


def test_no_model_and_no_default_rejected(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        json={"prompt": "p", "response_format": "b64_json"},
    )
    assert r.status_code == 400


def test_catalog_unavailable_skips_model_check(make_client):
    def handler(request):
        if request.url.path == "/api/v1/images/models":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=GENERATION_RESPONSE)

    tc, sent = make_client(handler)
    r = tc.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="brand-new/model"),
    )
    assert r.status_code == 200


def test_missing_prompt_rejected(client):
    r = client.post(
        "/v1/images/generations", headers=AUTH, json={"model": "openai/gpt-image-2"}
    )
    assert r.status_code == 400


def test_invalid_json_rejected(client):
    r = client.post(
        "/v1/images/generations",
        headers=AUTH,
        content=b"{nope",
    )
    assert r.status_code == 400


def test_upstream_error_wrapped(make_client):
    def handler(request):
        return httpx.Response(402, text="insufficient credits")

    tc, _ = make_client(handler)
    r = tc.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["type"] == "upstream_error"
    assert err["code"] == 402
    assert "credits" in err["message"]


def test_openai_shaped_upstream_error_passthrough(make_client):
    upstream_body = {"error": {"message": "blocked", "type": "moderation"}}

    def handler(request):
        return httpx.Response(403, json=upstream_body)

    tc, _ = make_client(handler)
    r = tc.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 403
    assert r.json() == upstream_body


def test_429_passthrough(make_client):
    upstream_body = {"error": {"message": "rate limited", "type": "rate_limit"}}

    def handler(request):
        return httpx.Response(429, json=upstream_body)

    tc, _ = make_client(handler)
    r = tc.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 429
    assert r.json() == upstream_body


def test_upstream_timeout_504(make_client):
    def handler(request):
        raise httpx.TimeoutException("timed out")

    tc, _ = make_client(handler)
    r = tc.post(
        "/v1/images/generations",
        headers=AUTH,
        json=gen_body(model="openai/gpt-image-2"),
    )
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "upstream_timeout"


def test_edits_input_references_and_fields(client):
    r = client.post(
        "/v1/images/edits",
        headers=AUTH,
        data={
            "model": "google/gemini-3.1-flash-image",
            "prompt": "make it red",
            "n": "2",
            "size": "1024x1024",
            "background": "transparent",
            "response_format": "b64_json",
        },
        files=[
            ("image[]", ("a.png", PNG, "image/png")),
            ("image[]", ("b.jpg", b"\xff\xd8ff", "image/jpeg")),
            ("image", ("c.png", b"\x89PNG", "")),  # blank content type -> png default
        ],
    )
    assert r.status_code == 200
    payload = sent_payload(client.sent)
    refs = payload["input_references"]
    assert len(refs) == 3  # received order preserved
    assert refs[0] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{PNG_B64}"},
    }
    assert refs[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert refs[2]["image_url"]["url"].startswith("data:image/png;base64,")  # default
    assert payload["n"] == 2
    assert payload["size"] == "1024x1024"
    assert payload["background"] == "transparent"
    assert "response_format" not in payload
    assert r.json()["data"][0]["b64_json"] == PNG_B64


def test_edits_requires_image(client):
    r = client.post(
        "/v1/images/edits",
        headers=AUTH,
        data={"model": "openai/gpt-image-2", "prompt": "p"},
    )
    assert r.status_code == 400


def test_edits_invalid_background_dropped(client):
    r = client.post(
        "/v1/images/edits",
        headers=AUTH,
        data={
            "model": "openai/gpt-image-2",
            "prompt": "p",
            "background": "blurry",
        },
        files=[("image", ("a.png", PNG, "image/png"))],
    )
    assert r.status_code == 200
    assert "background" not in sent_payload(client.sent)


def test_models_route_openai_shape(client):
    r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    by_id = {m["id"]: m for m in body["data"]}
    assert "google/gemini-3.1-flash-image" in by_id
    assert by_id["google/gemini-3.1-flash-image"]["object"] == "model"
    assert by_id["google/gemini-3.1-flash-image"]["created"] == 1700000000
    assert by_id["google/gemini-3.1-flash-image"]["owned_by"] == "google"


def test_health_ok(client):
    r = client.get("/health", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_degraded_when_upstream_auth_fails(make_client):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    tc, _ = make_client(handler)
    assert tc.get("/health", headers=AUTH).status_code == 502


def test_image_route_404_for_unknown_id(client):
    assert client.get("/v1/images/nope", headers=AUTH).status_code == 404