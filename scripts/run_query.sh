#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <file.sql> '<start YYYY-MM-DD HH:MM:SS>' '<end YYYY-MM-DD HH:MM:SS>'  (UTC)" >&2
  exit 1
fi

docker exec -i clickhouse clickhouse-client --password sentinel_dev \
  --param_start="$2" --param_end="$3" < "$1"
