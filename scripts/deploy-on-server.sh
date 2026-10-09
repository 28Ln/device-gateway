#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-/opt/btf-device-gateway}
NEW_API_DIR=${NEW_API_DIR:-/opt/new-api}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-http://127.0.0.1:8080}

cd "$PROJECT_DIR"

if [[ ! -d "$NEW_API_DIR" ]]; then
  echo "missing $NEW_API_DIR" >&2
  exit 1
fi

relay_token=$(
  cd "$NEW_API_DIR"
  docker compose exec -T postgres psql -U new_api -d new_api -Atc \
    'select "key" from tokens where status=1 order by id limit 1;' < /dev/null
)

if [[ -z "$relay_token" ]]; then
  echo "could not read New API relay token" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  umask 077
  cat > .env <<EOF
PUBLIC_BASE_URL=$PUBLIC_BASE_URL
NEW_API_BASE_URL=http://new-api:3000
NEW_API_TOKEN=$relay_token
DEMO_PAYMENT_ENABLED=false
MOCK_PAYMENT_SECRET=
PAYMENT_NOTIFY_URL=
WECHAT_APP_ID=
WECHAT_MCH_ID=
WECHAT_SERIAL_NO=
WECHAT_PLATFORM_SERIAL_NO=
WECHAT_PRIVATE_KEY=
WECHAT_API_V3_KEY=
WECHAT_PLATFORM_PUBLIC_KEY=
WECHAT_API_BASE_URL=https://api.mch.weixin.qq.com
ALIPAY_APP_ID=
ALIPAY_PRIVATE_KEY=
ALIPAY_PUBLIC_KEY=
ALIPAY_GATEWAY=https://openapi.alipay.com/gateway.do
EOF
fi

docker compose build
docker compose up -d
docker compose exec -T btf-device-gateway python -m app.cli init-db
