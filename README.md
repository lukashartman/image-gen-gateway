# OpenRouter → OpenAI Image Generation Gateway

A standalone Python service that speaks the **OpenAI Images API** and
translates to **OpenRouter's dedicated Image API** (`POST /api/v1/images`),
so OpenRouter image models can be used through **Open WebUI's native Image
Generation and Image Edit features with zero fork changes**.

- `POST /v1/images/generations` — OpenAI JSON shape → OpenRouter `POST /api/v1/images`
- `POST /v1/images/edits` — multipart (`image` / `image[]` parts) → OpenRouter `input_references`
- `GET /v1/models` — OpenAI-shaped model list built from OpenRouter's `/api/v1/images/models` (cached 1h)
- `GET /health` — liveness plus a free upstream auth probe (doubles as "verify connection")
- `GET /v1/images/{id}` — URL-mode blob served from an in-memory TTL store

Every route requires `Authorization: Bearer <key>` (401 otherwise, nothing is
forwarded upstream).

## Open WebUI setup (Settings → Admin → Images)

| Setting | Value |
|---|---|
| Image Engine | **Default (Open AI)** |
| API Base URL | `http://openrouter-image-gateway:8400/v1` |
| API Key | an OpenRouter API key |
| Model | any OpenRouter image slug, typed into the free-text box (e.g. `google/gemini-3.1-flash-image`) |

Notes:

- Open WebUI's model dropdown/`verify connection` are served by Open WebUI's
  own backend (static list for this engine) — the box is free-text, so any
  slug works. Use `MODEL_ALIASES` to map friendly names (e.g. `dall-e-3`) to
  OpenRouter slugs.
- Generation and editing both work from chat; edits attach the photo(s) from
  the message.

## Run

Docker (preferred, alongside Open WebUI on the same compose network — edit
the network name in `docker-compose.yml` to match, `docker network ls`):

```bash
docker compose up -d --build
```

Or locally:

```bash
pip install -r requirements.txt
python app.py   # listens on 0.0.0.0:8400
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | *(unset)* | Upstream key when `GATEWAY_API_KEY` mode is on. Startup refuses in that mode if unset. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Upstream base. |
| `HOST` / `PORT` | `0.0.0.0` / `8400` | Listen address. |
| `DEFAULT_MODEL` | *(unset)* | Used when a request omits `model`. |
| `MODEL_ALIASES` | `{}` | JSON map, e.g. `{"dall-e-3": "google/gemini-3.1-flash-image"}`. |
| `GATEWAY_API_KEY` | *(unset)* | Hardened mode: clients must present this key; upstream calls use `OPENROUTER_API_KEY`. Unset = passthrough mode. |
| `UPSTREAM_TIMEOUT` | `300` | Seconds (generations take 30–120s+). |
| `RESPONSE_URL_TTL` | `600` | Seconds for URL-mode blobs (capacity 32, oldest-first eviction). |
| `LOG_LEVEL` | `info` | Standard levels; `debug` also logs prompts. |

**Auth modes**

- *Passthrough* (`GATEWAY_API_KEY` unset): the presented bearer key is
  forwarded to OpenRouter as-is — Open WebUI's API key field holds the
  OpenRouter key. A passthrough gateway exposed publicly is an **open relay
  for your OpenRouter balance**; keep it on an internal compose network.
- *Hardened* (`GATEWAY_API_KEY` set): clients authenticate with that key,
  upstream calls use `OPENROUTER_API_KEY`.

Keys are never logged; prompts are logged only at debug level. Each request
logs model, image count, latency, and OpenRouter usage (tokens/USD).

## Translation rules

- `model` — aliases first, else raw slug; empty → `DEFAULT_MODEL` → 400.
  Unknown-to-catalog slugs → 400 naming `GET /v1/models` (catalog cached 1h;
  if the catalog can't be fetched the check is skipped).
- `n` — clamped to 1–10 (single-image providers may reject `n>1`; that
  upstream error is surfaced as-is; aggregation is future work).
- `size` — `"WxH"` pixels or a tier (`512`, `768`, `1K`, `1.5K`, `2K`, `4K`)
  forwarded; `"auto"`/empty/malformed dropped. When a size is forwarded,
  `resolution`/`aspect_ratio` are dropped (OpenRouter 400s on the combination).
- `quality` — `auto|low|medium|high` pass through; `standard`→`medium`,
  `hd`→`high`; else dropped.
- `background` (edits) — `auto|transparent|opaque` pass through; else dropped.
- Extras (`IMAGES_OPENAI_API_PARAMS`) — only documented Image API fields are
  forwarded (`seed`, `aspect_ratio`, `resolution`, `output_format`,
  `background`, `output_compression`, `provider`, `user`); unknown keys are
  dropped, never blind-forwarded.
- `response_format` — shapes only the response: `b64_json` → `b64_json`
  passthrough (byte-for-byte); `"url"` **or absent** → the image is stored
  in-memory and returned as `<self>/v1/images/{id}` (auth still required).
- Upstream errors: OpenAI-shaped bodies pass through with the original
  status (incl. 429s); other bodies are wrapped as
  `{"error": {"message", "type": "upstream_error", "code": <status>}}`.
  Upstream timeout → 504.

## curl examples

```bash
KEY="sk-or-..."
BASE="http://localhost:8400"

# generate (b64)
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"google/gemini-3.1-flash-image","prompt":"a red panda","n":1,
       "size":"1024x1024","response_format":"b64_json"}' \
  "$BASE/v1/images/generations" | jq -r '.data[0].b64_json' | base64 -d > out.png

# edit (image-to-image)
curl -s -H "Authorization: Bearer $KEY" \
  -F "model=google/gemini-3.1-flash-image" -F "prompt=make it snowy" \
  -F "image=@photo.png;type=image/png" \
  "$BASE/v1/images/edits"

# catalog + health
curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models" | jq '.data[].id'
curl -s -H "Authorization: Bearer $KEY" "$BASE/health"
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/   # 46 tests, mocked upstream, no network
```

`scripts/smoke.sh` runs live against a running gateway (needs
`OPENROUTER_API_KEY`, ~$0.10):

```bash
OPENROUTER_API_KEY=sk-or-... GATEWAY_URL=http://localhost:8400 scripts/smoke.sh
```

## Known limitations

- **SVG uploads**: Open WebUI's uploader can't sniff SVG from raw base64, so
  Recraft vector outputs may fail to upload there; PNG/JPEG/WebP are unaffected.
- **`n>1`** on single-image providers is surfaced as an upstream error;
  sequential aggregation is future work.
- A just-added OpenRouter model may be rejected for up to 1h of catalog cache;
  the 400 tells you to retry or hit `GET /v1/models`.
- Providers may clamp a requested pixel size to their supported set (same
  aspect ratio, different pixels).
- Per-model parameter support varies; the gateway doesn't pre-validate and
  lets upstream errors surface (capability details exist at
  `/api/v1/images/models/{id}/endpoints`).
- Memory-bound per request (multi-MB base64 flows through JSON/multipart);
  sized for a single-admin deployment.