import gzip
import os
from pathlib import Path

import clickhouse_connect
import pytest
from clickhouse_connect.driver.exceptions import OperationalError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "tests" / "schema"
FIXTURES_DIR = ROOT / "fixtures"

TEST_DB = "otel_test"
TABLES = ("otel_traces", "otel_logs")

ADMIN_USER = os.getenv("SENTINEL_TEST_ADMIN_USER", "default")
ADMIN_PASSWORD = os.getenv("SENTINEL_TEST_ADMIN_PASSWORD", "sentinel_dev")


@pytest.fixture(scope="session")
def admin_client():
    """A ClickHouse client with write access, or skip every test that needs it."""
    try:
        client = clickhouse_connect.get_client(
            host="localhost", port=8123, username=ADMIN_USER, password=ADMIN_PASSWORD
        )
    except OperationalError as exc:
        pytest.skip(f"ClickHouse is not reachable at localhost:8123, skipping database tests ({exc})")
    yield client
    client.close()


def _create_statement(table: str) -> str:
    """The production CREATE TABLE, pointed at the test database and without its TTL."""
    sql = (SCHEMA_DIR / f"{table}.sql").read_text()
    sql = sql.replace("CREATE TABLE otel.", f"CREATE TABLE {TEST_DB}.")
    return "\n".join(line for line in sql.splitlines() if not line.startswith("TTL "))


@pytest.fixture(scope="session")
def test_db(admin_client):
    """Create empty otel_test tables and point the tools at them for the whole session."""
    admin_client.command(f"CREATE DATABASE IF NOT EXISTS {TEST_DB}")
    for table in TABLES:
        admin_client.command(f"DROP TABLE IF EXISTS {TEST_DB}.{table}")
        admin_client.command(_create_statement(table))

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SENTINEL_CH_DATABASE", TEST_DB)
        mp.setenv("SENTINEL_CH_USER", ADMIN_USER)
        mp.setenv("SENTINEL_CH_PASSWORD", ADMIN_PASSWORD)
        yield


def _insert_columns(client, table: str) -> list[str]:
    """Columns a SELECT * export contains: all except MATERIALIZED and ALIAS, in table order."""
    rows = client.query(
        "SELECT name FROM system.columns"
        " WHERE database = {db:String} AND table = {table:String}"
        " AND default_kind NOT IN ('MATERIALIZED', 'ALIAS')"
        " ORDER BY position",
        parameters={"db": TEST_DB, "table": table},
    ).result_rows
    return [name for (name,) in rows]


@pytest.fixture(scope="session")
def load_incident(admin_client, test_db):
    """Returns load(name): empties both test tables, then loads that incident's fixtures."""
    columns = {table: _insert_columns(admin_client, table) for table in TABLES}

    def load(name: str) -> None:
        for table in TABLES:
            admin_client.command(f"TRUNCATE TABLE {TEST_DB}.{table}")
        for table in TABLES:
            data = gzip.decompress((FIXTURES_DIR / f"{name}.{table}.native.gz").read_bytes())
            admin_client.raw_insert(
                f"{TEST_DB}.{table}", column_names=columns[table], insert_block=data, fmt="Native"
            )

    return load
