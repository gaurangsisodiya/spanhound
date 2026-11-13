import os
from datetime import datetime, timedelta
from pathlib import Path

import clickhouse_connect

QUERIES_DIR = Path(__file__).resolve().parents[2] / "queries"
MAX_WINDOW = timedelta(minutes=60)


def get_client():
    return clickhouse_connect.get_client(
        host=os.getenv("SPANHOUND_CH_HOST", "localhost"),
        port=int(os.getenv("SPANHOUND_CH_PORT", "8123")),
        username=os.getenv("SPANHOUND_CH_USER", "spanhound_ro"),
        password=os.getenv("SPANHOUND_CH_PASSWORD", "sentinel_ro_dev"),
        database=os.getenv("SPANHOUND_CH_DATABASE", "otel"),
    )


def check_window(start: str, end: str) -> None:
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if e <= s:
        raise ValueError(f"end ({end}) must be after start ({start})")
    if e - s > MAX_WINDOW:
        raise ValueError(f"window is {e - s}; the maximum is {MAX_WINDOW}")


def run_query(name: str, **params) -> list[tuple]:
    sql = (QUERIES_DIR / f"{name}.sql").read_text()
    return get_client().query(sql, parameters=params).result_rows