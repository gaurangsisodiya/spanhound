import pytest
from clickhouse_connect.driver.exceptions import OperationalError

from spanhound.fixtures import (
    TEST_DB,
    admin_client as make_admin_client,
    admin_credentials,
    create_test_tables,
    fixture_loader,
)

ADMIN_USER, ADMIN_PASSWORD = admin_credentials()


@pytest.fixture(scope="session")
def admin_client():
    """A ClickHouse client with write access, or skip every test that needs it."""
    try:
        client = make_admin_client()
    except OperationalError as exc:
        pytest.skip(f"ClickHouse is not reachable at localhost:8123, skipping database tests ({exc})")
    yield client
    client.close()


@pytest.fixture(scope="session")
def test_db(admin_client):
    """Create empty otel_test tables and point the tools at them for the whole session."""
    create_test_tables(admin_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SPANHOUND_CH_DATABASE", TEST_DB)
        mp.setenv("SPANHOUND_CH_USER", ADMIN_USER)
        mp.setenv("SPANHOUND_CH_PASSWORD", ADMIN_PASSWORD)
        yield


@pytest.fixture(scope="session")
def load_incident(admin_client, test_db):
    """Returns load(name): empties both test tables, then loads that incident's fixtures."""
    return fixture_loader(admin_client)
