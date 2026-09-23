#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BTF_BASE_URL:-http://127.0.0.1}
DEVICE_KEY=${BTF_DEVICE_KEY:-}
MODEL=${BTF_MODEL:-deepseek-v4.1-flash}

if [[ -z "$DEVICE_KEY" ]]; then
  echo "BTF_DEVICE_KEY is required" >&2
  exit 2
fi

json_get() {
  curl -fsS --max-time 20 "$1"
}

echo "health"
json_get "$BASE_URL/health" | jq -e '.ok == true' >/dev/null

echo "new-api status"
json_get "$BASE_URL/api/status" | jq -e '.success == true' >/dev/null

echo "device status"
curl -fsS --max-time 20 "$BASE_URL/api/device/status" \
  -H "Authorization: Bearer $DEVICE_KEY" |
  jq -e '.device_id and .status' >/dev/null

echo "models"
curl -fsS --max-time 30 "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $DEVICE_KEY" |
  jq -e --arg model "$MODEL" '.data | map(.id) | index($model) != null' >/dev/null

if [[ "${RUN_CHAT:-0}" == "1" ]]; then
  echo "chat completion"
  curl -fsS --max-time 120 "$BASE_URL/v1/chat/completions" \
    -H "Authorization: Bearer $DEVICE_KEY" \
    -H "Content-Type: application/json" \
    --data "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: BTF_SMOKE_OK\"}],\"max_tokens\":8,\"stream\":false}" |
    jq -e '.choices[0].message.content' >/dev/null
fi

echo "smoke test passed"
