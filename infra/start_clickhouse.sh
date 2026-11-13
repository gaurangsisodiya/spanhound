#!/usr/bin/env bash
# (Re)create the ClickHouse container. Run this after editing
# infra/clickhouse/spanhound_ro.xml: a plain restart can keep the old file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RO_USER_XML="$SCRIPT_DIR/clickhouse/spanhound_ro.xml"

if [ ! -f "$RO_USER_XML" ]; then
  echo "Missing $RO_USER_XML" >&2
  exit 1
fi

docker rm -f clickhouse >/dev/null 2>&1 || true

# The demo's network; created here if missing so ClickHouse can start without the demo.
docker network inspect opentelemetry-demo >/dev/null 2>&1 || docker network create opentelemetry-demo

docker run -d \
  --name clickhouse \
  --network opentelemetry-demo \
  --restart unless-stopped \
  --ulimit nofile=262144:262144 \
  -e CLICKHOUSE_DB=otel \
  -e CLICKHOUSE_USER=default \
  -e CLICKHOUSE_PASSWORD=sentinel_dev \
  -p 127.0.0.1:8123:8123 \
  -v clickhouse-data:/var/lib/clickhouse \
  -v "$RO_USER_XML":/etc/clickhouse-server/users.d/spanhound_ro.xml:ro \
  clickhouse/clickhouse-server:latest >/dev/null

for _ in $(seq 1 30); do
  if docker exec clickhouse clickhouse-client --password sentinel_dev -q "SELECT 1" >/dev/null 2>&1; then
    echo "ClickHouse ready"
    exit 0
  fi
  sleep 1
done

echo "ClickHouse did not answer within 30 seconds. Last log lines:" >&2
docker logs --tail 20 clickhouse >&2
exit 1
