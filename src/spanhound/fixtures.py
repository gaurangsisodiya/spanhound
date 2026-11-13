"""Load saved incident fixtures into the otel_test database (used by tests and the eval)."""

import gzip
import os
from collections.abc import Callable
from pathlib import Path

import clickhouse_connect

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "tests" / "schema"
FIXTURES_DIR = ROOT / "fixtures"

TEST_DB = "otel_test"
TABLES = ("otel_traces", "otel_logs")


def admin_credentials() -> tuple[str, str]:
    """(user, password) with write access. Read at call time, so a loaded .env counts."""
    return (
        os.getenv("SPANHOUND_TEST_ADMIN_USER", "default"),
        os.getenv("SPANHOUND_TEST_ADMIN_PASSWORD", "sentinel_dev"),
    )


def admin_client():
    """A ClickHouse client with write access. Raises OperationalError if unreachable."""
    user, password = admin_credentials()
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username=user, password=password
    )


def create_statement(table: str) -> str:
    """The production CREATE TABLE, pointed at the test database and without its TTL."""
    sql = (SCHEMA_DIR / f"{table}.sql").read_text()
    sql = sql.replace("CREATE TABLE otel.", f"CREATE TABLE {TEST_DB}.")
    return "\n".join(line for line in sql.splitlines() if not line.startswith("TTL "))


def create_test_tables(client) -> None:
    """(Re)create empty otel_test tables from tests/schema."""
    client.command(f"CREATE DATABASE IF NOT EXISTS {TEST_DB}")
    for table in TABLES:
        client.command(f"DROP TABLE IF EXISTS {TEST_DB}.{table}")
        client.command(create_statement(table))


def insert_columns(client, table: str) -> list[str]:
    """Columns a SELECT * export contains: all except MATERIALIZED and ALIAS, in table order."""
    rows = client.query(
        "SELECT name FROM system.columns"
        " WHERE database = {db:String} AND table = {table:String}"
        " AND default_kind NOT IN ('MATERIALIZED', 'ALIAS')"
        " ORDER BY position",
        parameters={"db": TEST_DB, "table": table},
    ).result_rows
    return [name for (name,) in rows]


def fixture_loader(client) -> Callable[[str], None]:
    """Returns load(name): empties both test tables, then loads that incident's fixtures."""
    columns = {table: insert_columns(client, table) for table in TABLES}

    def load(name: str) -> None:
        for table in TABLES:
            client.command(f"TRUNCATE TABLE {TEST_DB}.{table}")
        for table in TABLES:
            data = gzip.decompress((FIXTURES_DIR / f"{name}.{table}.native.gz").read_bytes())
            client.raw_insert(
                f"{TEST_DB}.{table}", column_names=columns[table], insert_block=data, fmt="Native"
            )

    return load
