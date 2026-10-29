"""OpenRouter -> OpenAI Image Generation Gateway.

Accepts generic OpenAI Images API requests (as sent by Open WebUI's
"Default (Open AI)" image engine), translates them to OpenRouter's dedicated
Image API (POST /api/v1/images), and returns OpenAI-shaped responses.

Routes:
    POST /v1/images/generations   OpenAI JSON shape -> OpenRouter images
    POST /v1/images/edits         multipart -> OpenRouter input_references
    GET  /v1/models               OpenAI-shaped model list (cached 1h)
    GET  /v1/images/{image_id}    URL-mode blob served from an in-memory TTL store
    GET  /health                  liveness + free upstream auth probe

Every route requires `Authorization: Bearer <key>`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.formparsers import UploadFile

log = logging.getLogger("gateway")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _load_json_env(name: str, default: Any) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("env %s is not valid JSON; using default %r", name, default)
        return default


class Settings:
    def __init__(self) -> None:
        env = os.environ.get
        self.openrouter_api_key = env("OPENROUTER_API_KEY", "")
        self.openrouter_base_url = env(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self.host = env("HOST", "0.0.0.0")
        self.port = int(env("PORT", "8400"))
        self.default_model = env("DEFAULT_MODEL", "").strip()
        self.model_aliases: dict = _load_json_env("MODEL_ALIASES", {}) or {}
        self.gateway_api_key = env("GATEWAY_API_KEY", "")
        self.upstream_timeout = float(env("UPSTREAM_TIMEOUT", "300"))
        self.response_url_ttl = float(env("RESPONSE_URL_TTL", "600"))
        self.log_level = env("LOG_LEVEL", "info").lower()


settings = Settings()
log.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

# --------------------------------------------------------------------------
# Translation tables
# --------------------------------------------------------------------------

CATALOG_TTL = 3600.0  # seconds; brief: cached 1h
STORE_CAPACITY = 32  # URL-mode blobs, oldest-first eviction

PIXEL_SIZE_RE = re.compile(r"^\d{1,5}x\d{1,5}$")
SIZE_TIERS_RE = re.compile(r"^(512|768|1K|1\.5K|2K|4K)$", re.IGNORECASE)
VALID_QUALITIES = {"auto", "low", "medium", "high"}
QUALITY_ALIASES = {"standard": "medium", "hd": "high"}
VALID_BACKGROUNDS = {"auto", "transparent", "opaque"}
# Only documented OpenRouter Image API fields are forwarded (never blind).
EXTRA_ALLOWLIST = {
    "seed",
    "aspect_ratio",
    "resolution",
    "output_format",
    "background",
    "output_compression",
    "provider",
    "user",
}
# OpenRouter rejects an explicit size alongside resolution/aspect_ratio.
SIZE_CONFLICT_EXTRAS = {"resolution", "aspect_ratio"}
# Fields we consume explicitly and must not also treat as extras.
GEN_CONSUMED = {"model", "prompt", "n", "size", "quality", "response_format"}
EDITS_CONSUMED = GEN_CONSUMED | {"background"}

DATA_URI_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)?,", re.IGNORECASE)


# --------------------------------------------------------------------------
# Errors (OpenAI-shaped)
# --------------------------------------------------------------------------


class ApiError(Exception):
    """Raises an OpenAI-shaped error response."""

    def __init__(
        self,
        status: int,
        message: str,
        type_: str = "invalid_request_error",
        code: Any = None,
        param: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.param = param

    def body(self) -> dict:
        err: dict = {"message": self.message, "type": self.type}
        if self.param:
            err["param"] = self.param
        if self.code is not None:
            err["code"] = self.code
        return {"error": err}


# --------------------------------------------------------------------------
# Pure translation helpers
# --------------------------------------------------------------------------


def normalize_size(size: Any) -> Optional[str]:
    """Return a size OpenRouter accepts, or None to drop it."""
    if size is None:
        return None
    s = str(size).strip()
    if not s or s.lower() == "auto":
        return None
    if PIXEL_SIZE_RE.fullmatch(s):
        return s
    if SIZE_TIERS_RE.fullmatch(s):
        return s.upper()  # canonical tier, e.g. "2k" -> "2K"
    log.debug("dropping unsupported size %r", size)
    return None


def normalize_quality(quality: Any) -> Optional[str]:
    if quality is None:
        return None
    q = str(quality).strip().lower()
    if q in VALID_QUALITIES:
        return q
    if q in QUALITY_ALIASES:  # standard -> medium, hd -> high
        return QUALITY_ALIASES[q]
    log.debug("dropping unsupported quality %r", quality)
    return None


def normalize_background(background: Any) -> Optional[str]:
    if background is None:
        return None
    b = str(background).strip().lower()
    if b in VALID_BACKGROUNDS:
        return b
    log.debug("dropping unsupported background %r", background)
    return None


def normalize_n(n: Any) -> Optional[int]:
    """Clamp n into OpenRouter's 1..10 range; None/invalid -> omitted."""
    if n is None or n == "":
        return None
    try:
        value = int(n)
    except (TypeError, ValueError):
        log.debug("dropping non-integer n %r", n)
        return None
    return max(1, min(10, value))


def normalize_extras(extras: dict, size_forwarded: bool) -> dict:
    """Allowlist forwarded extras; unknown keys are dropped, never blind-sent."""
    out: dict = {}
    for key, value in extras.items():
        if key not in EXTRA_ALLOWLIST:
            log.debug("dropping non-allowlisted extra %r", key)
            continue
        if size_forwarded and key in SIZE_CONFLICT_EXTRAS:
            log.debug("dropping %r alongside explicit size (upstream 400s)", key)
            continue
        out[key] = value
    return out


def resolve_model_slug(model: Any) -> Optional[str]:
    """MODEL_ALIASES first, else the raw slug, else DEFAULT_MODEL, else None."""
    raw = model if isinstance(model, str) else ("" if model is None else str(model))
    raw = raw.strip()
    if raw:
        return str(settings.model_aliases.get(raw, raw))
    if settings.default_model:
        return settings.default_model
    return None


# --------------------------------------------------------------------------
# Upstream client / auth
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.gateway_api_key and not settings.openrouter_api_key:
        raise RuntimeError(
            "GATEWAY_API_KEY is set but OPENROUTER_API_KEY is empty; refusing to start."
        )
    app.state.http_client = httpx.AsyncClient()
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="openrouter-image-gateway", lifespan=lifespan)


async def get_upstream_client() -> httpx.AsyncClient:
    return app.state.http_client


async def require_upstream_key(request: Request) -> str:
    """Every route demands a bearer key; returns the key to use upstream."""
    header = request.headers.get("authorization") or ""
    scheme, _, key = header.partition(" ")
    key = key.strip()
    if scheme.lower() != "bearer" or not key:
        raise ApiError(
            401,
            "Missing API key. Send 'Authorization: Bearer <key>'.",
            code="invalid_api_key",
        )
    if settings.gateway_api_key:
        if key != settings.gateway_api_key:
            raise ApiError(
                401, "Invalid API key.", code="invalid_api_key"
            )
        if not settings.openrouter_api_key:
            raise ApiError(
                500,
                "GATEWAY_API_KEY is set but OPENROUTER_API_KEY is not configured.",
                type_="server_error",
            )
        return settings.openrouter_api_key
    return key  # passthrough mode: the presented key is the OpenRouter key


# --------------------------------------------------------------------------
# Model catalog (cached 1h)
# --------------------------------------------------------------------------


class Catalog:
    def __init__(self) -> None:
        self._items: Optional[list] = None
        self._expires = 0.0

    def reset(self) -> None:
        self._items, self._expires = None, 0.0

    async def fetch_items(
        self, client: httpx.AsyncClient, upstream_key: str
    ) -> Optional[list]:
        """Return cached catalog items, or None when unavailable (check skipped)."""
        now = time.time()
        if self._items is not None and now < self._expires:
            return self._items
        try:
            r = await client.get(
                f"{settings.openrouter_base_url}/images/models",
                headers={"Authorization": f"Bearer {upstream_key}"},
                timeout=30.0,
            )
            r.raise_for_status()
            items = r.json().get("data")
            if isinstance(items, list):
                self._items, self._expires = items, now + CATALOG_TTL
                return items
        except Exception as exc:
            log.debug("model catalog fetch failed: %s", exc)
        self.reset()
        return None

    async def fetch_ids(
        self, client: httpx.AsyncClient, upstream_key: str
    ) -> Optional[set]:
        items = await self.fetch_items(client, upstream_key)
        if items is None:
            return None
        ids = set()
        for item in items:
            if isinstance(item, dict):
                for key in ("id", "slug", "model"):
                    if item.get(key):
                        ids.add(str(item[key]))
                        break
        return ids


catalog = Catalog()


# --------------------------------------------------------------------------
# In-memory URL-mode blob store (TTL + capacity, oldest-first eviction)
# --------------------------------------------------------------------------


class ImageStore:
    def __init__(self, capacity: int = STORE_CAPACITY) -> None:
        self._items: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
        self.capacity = capacity

    def put(self, data: bytes, media_type: str, ttl: float) -> str:
        image_id = secrets.token_urlsafe(16)
        self._items[image_id] = (time.time() + ttl, data, media_type)
        self._items.move_to_end(image_id)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)  # evict oldest
        log.debug("stored url-mode image %s (%d bytes, ttl=%ss)", image_id, len(data), ttl)
        return image_id

    def get(self, image_id: str) -> Optional[tuple[bytes, str]]:
        item = self._items.get(image_id)
        if item is None:
            return None
        expires, data, media_type = item
        if time.time() >= expires:
            del self._items[image_id]
            return None
        return data, media_type


image_store = ImageStore()


# --------------------------------------------------------------------------
# Upstream calls
# --------------------------------------------------------------------------


async def post_images(
    client: httpx.AsyncClient, upstream_key: str, payload: dict
) -> httpx.Response:
    return await client.post(
        f"{settings.openrouter_base_url}/images",
        json=payload,
        headers={"Authorization": f"Bearer {upstream_key}"},
        timeout=settings.upstream_timeout,
    )


def upstream_error_response(r: httpx.Response) -> JSONResponse:
    """Pass through OpenAI-shaped upstream errors; wrap anything else."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and "error" in body:
        return JSONResponse(status_code=r.status_code, content=body)
    snippet = (r.text or "").strip()
    if len(snippet) > 500:
        snippet = snippet[:500] + "..."
    return JSONResponse(
        status_code=r.status_code,
        content={
            "error": {
                "message": snippet or f"upstream returned {r.status_code}",
                "type": "upstream_error",
                "code": r.status_code,
            }
        },
    )


# --------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------


async def resolve_and_check_model(
    client: httpx.AsyncClient, upstream_key: str, model: Any
) -> str:
    slug = resolve_model_slug(model)
    if not slug:
        raise ApiError(
            400,
            "'model' is required and no DEFAULT_MODEL is configured; "
            "pass any OpenRouter image slug (see GET /v1/models).",
            param="model",
        )
    ids = await catalog.fetch_ids(client, upstream_key)
    if ids is not None and slug not in ids:
        raise ApiError(
            400,
            f"model '{slug}' is not in the OpenRouter image catalog; "
            "it may be new (retry in <=1h) — see GET /v1/models for the list.",
            code="model_not_found",
            param="model",
        )
    return slug


def build_upstream_payload(
    slug: str,
    prompt: str,
    extras: dict,
    *,
    n: Any = None,
    size: Any = None,
    quality: Any = None,
    background: Any = None,
    input_references: Optional[list] = None,
) -> dict:
    """Shared field mapping for both routes. `extras` are pre-normalized."""
    payload: dict = {"model": slug, "prompt": prompt}
    size = normalize_size(size)
    n = normalize_n(n)
    quality = normalize_quality(quality)
    background = normalize_background(background)
    if n is not None:
        payload["n"] = n
    if size:
        payload["size"] = size
    if quality:
        payload["quality"] = quality
    if background is not None:
        payload["background"] = background
    payload.update(normalize_extras(extras, size_forwarded=bool(size)))
    if input_references is not None:
        payload["input_references"] = input_references
    return payload


# --------------------------------------------------------------------------
# Response translation
# --------------------------------------------------------------------------


def decode_data_uri(value: str) -> tuple[bytes, Optional[str]]:
    """Decode raw base64 or a data: URI; returns (bytes, media_type|None)."""
    media_type = None
    payload = value
    m = DATA_URI_RE.match(value)
    if m:
        media_type = m.group(1) or None
        payload = value.split(",", 1)[1] if "," in value else ""
    return base64.b64decode(payload), media_type


def translate_upstream_response(
    request: Request, upstream_body: dict, response_format: Any
) -> JSONResponse:
    fmt = (str(response_format) if response_format else "").strip().lower()
    if fmt not in ("url", "b64_json"):
        log.debug("unknown/absent response_format %r -> url mode", response_format)
        fmt = ""  # absent or unknown: URL mode (OpenAI default)

    data_in = upstream_body.get("data")
    if not isinstance(data_in, list) or not data_in:
        raise ApiError(
            502, "upstream returned no image data.", type_="upstream_error", code=502
        )

    items_out: list = []
    for item in data_in:
        if not isinstance(item, dict):
            raise ApiError(502, "upstream returned malformed image data.", type_="upstream_error")
        out: dict = {}
        media_type = item.get("media_type")
        if media_type:
            out["media_type"] = media_type  # preserved extra; harmless for OpenAI clients
        if fmt == "b64_json":
            if not item.get("b64_json"):
                raise ApiError(502, "upstream returned no b64_json.", type_="upstream_error")
            out["b64_json"] = item["b64_json"]  # byte-for-byte passthrough
        else:
            if not item.get("b64_json"):
                raise ApiError(502, "upstream returned no image data.", type_="upstream_error")
            data, uri_type = decode_data_uri(item["b64_json"])
            mt = media_type or uri_type or "image/png"
            image_id = image_store.put(data, mt, settings.response_url_ttl)
            out["url"] = f"{request.base_url}v1/images/{image_id}"
        items_out.append(out)

    created = upstream_body.get("created") or int(time.time())
    return JSONResponse(content={"created": created, "data": items_out})


# --------------------------------------------------------------------------
# Shared request handling
# --------------------------------------------------------------------------


async def handle_image_call(
    request: Request,
    upstream_key: str,
    client: httpx.AsyncClient,
    slug: str,
    payload: dict,
    response_format: Any,
) -> Response:
    """POST /images upstream, log, and translate the response."""
    started = time.perf_counter()
    try:
        r = await post_images(client, upstream_key, payload)
    except httpx.TimeoutException:
        log.warning("upstream timeout after %ss", settings.upstream_timeout)
        return JSONResponse(
            status_code=504,
            content={
                "error": {
                    "message": f"OpenRouter timed out after {settings.upstream_timeout:g}s.",
                    "type": "upstream_timeout",
                    "code": 504,
                }
            },
        )
    latency = time.perf_counter() - started
    body = None
    if r.status_code < 400:
        try:
            body = r.json()
        except ValueError:
            body = None
    if body is None:
        log.info(
            "images: model=%s status=%d latency=%.1fs",
            slug,
            r.status_code,
            latency,
        )
        return upstream_error_response(r)
    log.info(
        "images: model=%s n=%s images=%d latency=%.1fs usage=%s",
        slug,
        payload.get("n", 1),
        len(body.get("data") or []),
        latency,
        body.get("usage"),
    )
    return translate_upstream_response(request, body, response_format)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.get("/health")
async def health(
    upstream_key: str = Depends(require_upstream_key),
    client: httpx.AsyncClient = Depends(get_upstream_client),
) -> JSONResponse:
    try:
        r = await client.get(
            f"{settings.openrouter_base_url}/images/models",
            headers={"Authorization": f"Bearer {upstream_key}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "degraded", "upstream": f"{type(exc).__name__}: {exc}"},
        )
    if r.status_code >= 400:
        return JSONResponse(
            status_code=502,
            content={"status": "degraded", "upstream": f"HTTP {r.status_code}"},
        )
    return JSONResponse(content={"status": "ok", "upstream": "reachable"})


@app.get("/v1/models")
async def list_models(
    upstream_key: str = Depends(require_upstream_key),
    client: httpx.AsyncClient = Depends(get_upstream_client),
) -> JSONResponse:
    items = await catalog.fetch_items(client, upstream_key)
    if items is None:
        raise ApiError(
            502,
            "Could not fetch the model catalog from OpenRouter.",
            type_="upstream_error",
        )
    data = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("slug") or item.get("model") or "")
        if not model_id:
            continue
        created = item.get("created") or 0
        owned_by = item.get("owned_by") or (
            model_id.split("/", 1)[0] if "/" in model_id else "openrouter"
        )
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": owned_by,
            }
        )
    return JSONResponse(content={"object": "list", "data": data})


@app.post("/v1/images/generations")
async def images_generations(
    request: Request,
    upstream_key: str = Depends(require_upstream_key),
    client: httpx.AsyncClient = Depends(get_upstream_client),
) -> Response:
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "Request body must be a JSON object.")
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object.")

    prompt = body.get("prompt")
    if prompt is None or not str(prompt).strip():
        raise ApiError(400, "'prompt' is required.", param="prompt")
    prompt = str(prompt)
    log.debug("generations prompt: %s", prompt)

    slug = await resolve_and_check_model(client, upstream_key, body.get("model"))
    extras = {k: v for k, v in body.items() if k not in GEN_CONSUMED}
    payload = build_upstream_payload(
        slug,
        prompt,
        extras,
        n=body.get("n"),
        size=body.get("size"),
        quality=body.get("quality"),
    )
    return await handle_image_call(
        request, upstream_key, client, slug, payload, body.get("response_format")
    )


@app.post("/v1/images/edits")
async def images_edits(
    request: Request,
    upstream_key: str = Depends(require_upstream_key),
    client: httpx.AsyncClient = Depends(get_upstream_client),
) -> Response:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in content_type:
        raise ApiError(400, "Edits require a multipart/form-data request.")
    form = await request.form()

    # File parts may be named `image` (single) or `image[]` (multiple);
    # each becomes one input_references entry, in received order.
    input_references: list = []
    extras: dict = {}
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            data = await value.read()
            mt = (value.content_type or "").split(";", 1)[0].strip() or "image/png"
            input_references.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mt};base64,{base64.b64encode(data).decode('ascii')}"
                    },
                }
            )
        elif name not in EDITS_CONSUMED:
            extras[name] = value
    if not input_references:
        raise ApiError(
            400,
            "At least one image file part ('image' or 'image[]') is required.",
            param="image",
        )

    prompt = form.get("prompt")
    if prompt is None or not str(prompt).strip():
        raise ApiError(400, "'prompt' is required.", param="prompt")
    prompt = str(prompt)
    log.debug("edits prompt: %s", prompt)

    slug = await resolve_and_check_model(client, upstream_key, form.get("model"))
    payload = build_upstream_payload(
        slug,
        prompt,
        extras,
        n=form.get("n"),
        size=form.get("size"),
        background=form.get("background"),
        input_references=input_references,
    )
    return await handle_image_call(
        request, upstream_key, client, slug, payload, form.get("response_format")
    )


@app.get("/v1/images/{image_id}")
async def get_image(
    image_id: str,
    upstream_key: str = Depends(require_upstream_key),
    client: httpx.AsyncClient = Depends(get_upstream_client),
) -> Response:
    hit = image_store.get(image_id)
    if hit is None:
        raise ApiError(404, "Image not found or expired.", code="not_found")
    data, media_type = hit
    return Response(content=data, media_type=media_type)


if __name__ == "__main__":
    if settings.gateway_api_key and not settings.openrouter_api_key:
        raise SystemExit(
            "GATEWAY_API_KEY is set but OPENROUTER_API_KEY is empty; refusing to start."
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)