# Brief: OpenRouter → OpenAI Image Generation Gateway

**Date:** 2026-10-28
**Deliverable location:** `/home/user/openrouter-image-gateway/`
**Status:** Draft for review
**Classification:** Major (new standalone service; full brief → outline → deliverable path)

---

## 1. Problem

Open WebUI's native Image Generation ("Default (Open AI)" engine) speaks the OpenAI
Images API: `POST {base}/images/generations` and `POST {base}/images/edits`. OpenRouter
serves image models through its own dedicated Image API at `POST /api/v1/images` and
does not implement the OpenAI images routes — `POST /api/v1/images/edits` does not
exist (verified 404; image-to-image goes through the unified endpoint's
`input_references`). Pointing Open WebUI's `IMAGES_OPENAI_API_BASE_URL` at OpenRouter
therefore fails.

An alternative strategy — patching the Open WebUI fork to add OpenRouter as a native
engine — is documented in `open-webui/OPENROUTER_IMAGE_SUPPORT_PLAN.md` and is **out of
scope** here. The gateway approach works with unmodified Open WebUI and supersedes that
plan for this effort.

## 2. Goal and Success Criteria

Build a standalone Python gateway that accepts generic OpenAI Images API calls,
translates them to OpenRouter Image API calls, and returns generic OpenAI-shaped
responses, so OpenRouter image models can be used through Open WebUI's native Image
Generation and Image Edit features with zero fork changes.

Success is judged by all three of:

1. In Open WebUI (Settings → Admin → Images): engine = "Default (Open AI)", base URL =
   `http://openrouter-image-gateway:8400/v1`, API key = an OpenRouter API key, model =
   any OpenRouter image slug typed into the model box. A generated image appears in
   chat, and image editing works on an attached photo.
2. `curl` against the gateway alone reproduces generation and editing without Open
   WebUI in the loop.
3. `scripts/smoke.sh` passes against a live OpenRouter key.

## 3. Verified Facts (checked 2026-10-28)

**Open WebUI request/response contract** (from `open-webui/backend/open_webui/routers/images.py`,
OpenAI engine blocks in `image_generations()` and `image_edits()`):

- Generations: JSON body with `model`, `prompt`, `n`, `size` ("WxH" pixels, or "auto"
  from config), `response_format: "b64_json"` (omitted only when the configured model
  matches `IMAGE_URL_RESPONSE_MODELS_REGEX_PATTERN`), plus any `IMAGES_OPENAI_API_PARAMS`
  extras. Auth: `Authorization: Bearer <key>`.
- Edits: multipart POST with fields `model`, `prompt`, `n`, `size`, `background`,
  `response_format` and file parts named `image` (single) or `image[]` (multiple), each
  with filename and content type. Optional `?api-version=` query string must be
  tolerated.
- Response parsing (both routes): iterates `res["data"]`; each item is used via its
  `url` field (HTTP-fetched; the bearer header is forwarded only when the URL is
  same-origin with the configured base URL) or its `b64_json` field (raw base64 or a
  `data:` URI — both supported).
- Open WebUI raises on any non-2xx response, so error status codes must be meaningful.

**Open WebUI UI behavior:** the model dropdown and "verify connection" button are
served by Open WebUI's own backend, not the upstream. For the OpenAI engine,
`get_models()` returns a static list (dall-e-2, dall-e-3, gpt-image-1, gpt-image-1.5)
into a free-text input with a datalist — any slug can be typed manually — and
`verify_connection()` does not support the OpenAI engine at all. The gateway's
`GET /v1/models` and `GET /health` therefore serve generic clients and humans, not
Open WebUI.

**OpenRouter Image API** (from https://openrouter.ai/docs/guides/overview/multimodal/image-generation,
verified live against `GET /api/v1/images/models`):

- `POST /api/v1/images` accepts `model`, `prompt`, `n` (1–10), `size` (explicit "WxH"
  pixels — authoritative — or a tier like "2K"), `resolution`, `aspect_ratio`, `quality`
  (`auto|low|medium|high`), `output_format`, `background`, `output_compression`, `seed`,
  `stream`, `input_references`, `provider.*`, `user`. A mismatched `resolution`/
  `aspect_ratio` alongside an explicit `size` is rejected with 400.
- Response: `{"created", "data": [{"b64_json", "media_type"}], "usage"}`.
- Image editing: `input_references` entries of shape
  `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}`.
- Catalog via `GET /api/v1/images/models`: 61 models, including
  `google/gemini-2.5-flash-image`, `google/gemini-3.1-flash-image`,
  `google/gemini-3.1-flash-lite-image`, the `black-forest-labs/flux-3-image*` and
  `flux.2-*` families, `bytedance-seed/seedream-4.5` / `seedream-5-0-*`,
  `recraft/recraft-v3`/`v4*`, `openai/gpt-image-1`/`2`/`2.5-*`, `qwen/qwen-image-2.1-*`.
  New image models are added to this API exclusively; legacy image output via
  `/chat/completions` remains for older models only.
- Billing is all-or-nothing: failed generations are not billed.
- Generations take 30s–120s+ (the docs example reports 94s), so upstream timeouts must
  be generous.

## 4. Scope

**In:** the gateway service (`app.py`), `requirements.txt`, `Dockerfile`,
`docker-compose.yml` snippet, `README.md` (exact Open WebUI settings values, env
reference, curl examples), `tests/` (pytest, mocked upstream), `scripts/smoke.sh`
(live). Routes: `POST /v1/images/generations`, `POST /v1/images/edits`,
`GET /v1/models`, `GET /health`, plus the internal `GET /v1/images/{id}` used only for
URL-mode responses (§5.3). Translation targets OpenRouter's dedicated Image API only.

**Out (explicitly deferred to future work):** fork modifications, a
chat-completions fallback translator for legacy image models, streaming/partial
images, sequential aggregation when `n>1` is rejected, any Open WebUI UI changes.

## 5. Design

### 5.1 API Surface

`POST /v1/images/generations` — JSON, OpenAI shape. `POST /v1/images/edits` —
multipart as described in §3. `GET /v1/models` — OpenAI-shaped list
(`{"object":"list","data":[{"id","object":"model","created","owned_by"}]}`) built from
OpenRouter's `/api/v1/images/models`, cached 1h. `GET /health` — liveness plus a free
upstream auth probe (`GET /api/v1/images/models` with the resolved upstream key), so it
doubles as a "verify connection" check with no generation cost.

All routes require `Authorization: Bearer <key>`; a request without a key is rejected
with 401 before any processing (including `GET /v1/images/{id}` URL fetches — Open
WebUI fetches same-origin URLs with the bearer token attached, so this works).

### 5.2 Request Translation

Incoming parameters are normalized as follows (generations; edits use the same mapping
for shared fields):

| Incoming (OpenAI) | Outgoing (OpenRouter) | Rule |
|---|---|---|
| `model` | `model` | Resolve `MODEL_ALIASES` first, else pass raw slug. If empty/absent, use `DEFAULT_MODEL`; if neither, 400. If the resolved slug is absent from the cached image-model catalog, 400 naming the catalog (`GET /v1/models`). If the catalog itself cannot be fetched, skip the check and attempt the call. |
| `prompt` | `prompt` | Pass through unchanged. |
| `n` | `n` | Clamp to 1–10. |
| `size` | `size` | Pass through when "WxH" pixels or a valid tier (`512`, `768`, `1K`, `1.5K`, `2K`, `4K`); drop when "auto", empty, or malformed (debug log). When a `size` is forwarded, `resolution`/`aspect_ratio` arriving via extras are dropped (debug log) — OpenRouter rejects explicit size alongside them. |
| `quality` | `quality` | `auto/low/medium/high` pass through; `standard`→`medium`; `hd`→`high`; anything else dropped. |
| `response_format` | *(not forwarded)* | Shapes only our response (§5.3). |
| `background` (edits) | `background` | `auto/transparent/opaque` pass through; else dropped. |
| `IMAGES_OPENAI_API_PARAMS` extras | allowlisted | Forward only documented Image API fields: `seed`, `aspect_ratio`, `resolution`, `output_format`, `background`, `output_compression`, `provider`, `user`. Unknown keys dropped with a debug log (never blind-forwarded: unknown fields risk upstream 400s). |

Edits: each `image`/`image[]` file part becomes one `input_references` entry carrying
`data:<content-type>;base64,<payload>` in received order. `prompt`, `model`, `n`,
`size`, `background` map per the table. Multipart files may arrive as raw image bytes;
content type is taken from the part's declared type, defaulting to `image/png`.

### 5.3 Response Translation

Each OpenRouter `data[]` item becomes an OpenAI `data[]` item. `b64_json` is passed
through byte-for-byte as pure base64 (Open WebUI sniffs the MIME type itself from the
bytes). An extra `media_type` field from OpenRouter is preserved on the item (harmless
for generic clients). Top level: `created` passthrough; `usage` is logged, not returned
(keeps the response strictly OpenAI-shaped).

URL-mode rule (implementable as written): when `response_format` is `"url"` **or
absent** (Open WebUI omits it exactly when its `IMAGE_URL_RESPONSE_MODELS_REGEX_PATTERN`
matched and the caller wants URLs; generic OpenAI clients default to URLs), the gateway
decodes the image, stores it in an in-memory TTL store (`RESPONSE_URL_TTL`, default
600s; capacity 32 blobs with oldest-first eviction), and returns
`url: "<self>/v1/images/{id}"`. When `response_format` is `"b64_json"`, the b64 path
above applies. The image route requires the bearer token like all others.

Known limitation (documented in README): Open WebUI's uploader cannot sniff SVG from
raw base64, so Recraft vector outputs may fail to upload there; PNG/JPEG/WebP are
unaffected.

### 5.4 Error Translation

Upstream non-2xx: if the body is already OpenAI-shaped (`{"error": {...}}`), pass it
through with the original status; otherwise wrap as
`{"error": {"message": ..., "type": "upstream_error", "code": <status>}}`. Upstream
timeout (`UPSTREAM_TIMEOUT`, default 300s) → 504 with an error body. Missing key → 401.
Invalid request shape → 400. 429s pass through unchanged.

### 5.5 Configuration and Auth

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | *(unset)* | Upstream key when `GATEWAY_API_KEY` mode is on. Refuses startup in that mode if unset. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Upstream base. |
| `HOST` / `PORT` | `0.0.0.0` / `8400` | Listen address. |
| `DEFAULT_MODEL` | *(unset)* | Used when a request omits `model`. |
| `MODEL_ALIASES` | `{}` | JSON map, e.g. `{"dall-e-3": "google/gemini-3.1-flash-image", "gpt-image-1": "openai/gpt-image-2"}`. |
| `GATEWAY_API_KEY` | *(unset)* | Optional hardening (below). |
| `UPSTREAM_TIMEOUT` | `300` | Seconds. |
| `RESPONSE_URL_TTL` | `600` | Seconds for URL-mode blobs. |
| `LOG_LEVEL` | `info` | Standard levels. |

Auth rule: every request must present `Authorization: Bearer <key>`; a missing or empty
key is rejected with 401 and nothing is forwarded upstream. There is **no** env-var
fallback for absent credentials. When `GATEWAY_API_KEY` is set, the presented key must
match it (else 401) and upstream calls use `OPENROUTER_API_KEY`; when it is unset, the
presented key is passed through to OpenRouter as-is (Open WebUI's API key field holds
the OpenRouter key).

Logging records model, image count, latency, and OpenRouter's `usage` (tokens and USD
cost) at info level. Keys are never logged; prompt text only at debug level. The README
warns that a passthrough gateway exposed publicly is an open relay for the OpenRouter
balance and belongs on an internal compose network.

### 5.6 Packaging and Deployment

```
openrouter-image-gateway/
  app.py              # the entire service (FastAPI + httpx + uvicorn), sectioned
  requirements.txt    # pinned: fastapi, uvicorn, httpx, python-multipart
  Dockerfile          # python:3.12-slim
  docker-compose.yml  # snippet for infrastructure/, same Docker network as Open WebUI
  README.md           # Open WebUI settings values, env reference, curl examples
  tests/              # pytest, mocked upstream (no network)
  scripts/smoke.sh    # live end-to-end script (needs OPENROUTER_API_KEY)
```

The compose snippet defines service `openrouter-image-gateway` on port 8400, joining
the same Docker network as Open WebUI so the base URL
`http://openrouter-image-gateway:8400/v1` resolves from the Open WebUI container.

### 5.7 Risks and Unknowns

- Providers clamp sizes to supported sets; a requested pixel size may return at the
  same aspect ratio but different pixels. Acceptable; verified live in smoke tests.
- `n>1`: single-image providers reject it. v1 passes `n` through and surfaces the
  upstream error; sequential aggregation is future work.
- Stale model-catalog cache (1h TTL) may briefly reject a just-added model; the error
  message tells the user to retry or hit `GET /v1/models`.
- Large payloads (multi-MB base64) flow through JSON responses and multipart uploads;
  the gateway is memory-bound per request and sized for a single-admin deployment.
- OpenRouter's per-provider parameter support varies (capability descriptors exist at
  `/api/v1/images/models/{id}/endpoints`); the gateway does not pre-validate per model
  and lets upstream errors surface.

### 5.8 Testing and Success Judgment

`tests/` covers every mapping function (size/quality normalization, extras allowlist,
edits → `input_references`, response shaping incl. URL mode, error wrapping, auth
rejection) against a mocked httpx transport, plus fixture tests replaying the exact
request shapes Open WebUI's `images.py` sends.

`scripts/smoke.sh` runs live: `GET /health`, `GET /v1/models`, generations on three
catalog models (`google/gemini-3.1-flash-image`, `openai/gpt-image-2`,
`bytedance-seed/seedream-5-0-flash`), one edit with a tiny PNG, and a bogus-model 400.
Expected cost on the order of $0.10.

Final success = smoke passes **and** generation/edit work inside a chat in the real
Open WebUI instance (user-judged last mile).

## 6. Assumptions

- The gateway replaces the fork-patch strategy; Open WebUI stays unmodified.
- Deliverable lives at `/home/user/openrouter-image-gateway/` (change on request).
- "Full surface" means the four gateway routes in §5.1. Open WebUI's own dropdown will
  not list OpenRouter models (its static list is served by its backend); free-text
  entry plus `MODEL_ALIASES` is the mitigation.
- Docker deployment alongside the existing Open WebUI container on the same host /
  compose network.

## 7. Future Work (deferred)

- Chat-completions fallback translator for legacy chat-only image models (same
  translator seam).
- Sequential single-image aggregation for `n>1` on limited providers.
- SSE streaming with partial-image progress.
- `GET /v1/images/models/{id}` detail passthrough.