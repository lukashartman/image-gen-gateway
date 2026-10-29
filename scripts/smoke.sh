#!/usr/bin/env bash
# Live end-to-end smoke test for the OpenRouter image gateway.
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-...        # (or GATEWAY_API_KEY in hardened mode)
#   GATEWAY_URL=http://localhost:8400 scripts/smoke.sh
#
# Expects the gateway to be running (GATEWAY_API_KEY passthrough mode by
# default, i.e. the key above is forwarded to OpenRouter). Expected cost is
# on the order of $0.10.
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8400}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-${GATEWAY_API_KEY:-}}"

if [ -z "$OPENROUTER_API_KEY" ]; then
  echo "FAIL: export OPENROUTER_API_KEY (or GATEWAY_API_KEY) first" >&2
  exit 1
fi
command -v curl >/dev/null || { echo "FAIL: curl is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "FAIL: jq is required" >&2; exit 1; }

AUTH=("Authorization: Bearer $OPENROUTER_API_KEY")
JSON="Content-Type: application/json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. Health (liveness + upstream auth probe)
curl -fsS -H "${AUTH[0]}" "$GATEWAY_URL/health" | jq -e '.status == "ok"' >/dev/null \
  || fail "GET /health"
pass "GET /health"

# 2. Model catalog
curl -fsS -H "${AUTH[0]}" "$GATEWAY_URL/v1/models" \
  | jq -e '.object == "list" and (.data | length > 0)' >/dev/null \
  || fail "GET /v1/models"
pass "GET /v1/models"

# 3. Generations on three catalog models
MODELS=(
  google/gemini-3.1-flash-image
  openai/gpt-image-2
  bytedance-seed/seedream-5-0-flash
)
for model in "${MODELS[@]}"; do
  resp=$(curl -fsS -H "${AUTH[0]}" -H "$JSON" \
    -d "{\"model\":\"$model\",\"prompt\":\"a small green circle on a white background\",\"n\":1,\"size\":\"auto\",\"response_format\":\"b64_json\"}" \
    "$GATEWAY_URL/v1/images/generations") \
    || fail "generations: $model"
  echo "$resp" | jq -e '.data[0].b64_json | length > 100' >/dev/null \
    || fail "generations response for $model"
  pass "generations: $model"
done

# 4. One edit (image-to-image) with a tiny PNG
printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==' \
  | base64 -d > "$TMP/tiny.png"
resp=$(curl -fsS -H "${AUTH[0]}" \
  -F "model=google/gemini-3.1-flash-image" \
  -F "prompt=make the image red" \
  -F "n=1" \
  -F "size=auto" \
  -F "background=transparent" \
  -F "response_format=b64_json" \
  -F "image=@$TMP/tiny.png;type=image/png" \
  "$GATEWAY_URL/v1/images/edits") \
  || fail "edits"
echo "$resp" | jq -e '.data[0].b64_json | length > 100' >/dev/null \
  || fail "edits response"
pass "edits: google/gemini-3.1-flash-image"

# 5. URL-mode round trip (response_format omitted -> url -> fetch)
resp=$(curl -fsS -H "${AUTH[0]}" -H "$JSON" \
  -d '{"model":"google/gemini-3.1-flash-image","prompt":"a blue square","n":1}' \
  "$GATEWAY_URL/v1/images/generations") \
  || fail "url-mode generations"
url=$(echo "$resp" | jq -r '.data[0].url')
[ -n "$url" ] || fail "url-mode generations returned no url"
curl -fsS -H "${AUTH[0]}" "$url" -o "$TMP/url.png" || fail "fetch url-mode image ($url)"
file_size=$(wc -c < "$TMP/url.png")
[ "$file_size" -gt 100 ] || fail "url-mode image too small ($file_size bytes)"
pass "url-mode round trip"

# 6. A bogus model must be rejected with 400 naming the catalog
code=$(curl -s -o "$TMP/bogus.json" -w '%{http_code}' -H "${AUTH[0]}" -H "$JSON" \
  -d '{"model":"not/a-real-model","prompt":"x"}' \
  "$GATEWAY_URL/v1/images/generations")
[ "$code" = "400" ] || fail "bogus model returned $code, expected 400"
grep -q '/v1/models' "$TMP/bogus.json" || fail "bogus-model error does not name GET /v1/models"
pass "bogus model -> 400 naming GET /v1/models"

echo
echo "All smoke checks passed."