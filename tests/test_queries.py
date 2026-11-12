"""Static checks on queries/: filters the tools rely on."""

from pathlib import Path

import pytest

QUERIES = Path(__file__).resolve().parents[1] / "queries"


@pytest.mark.parametrize("name", ["error_origins", "slow_operations", "error_logs", "error_rate"])
def test_load_generator_is_excluded(name):
    sql = (QUERIES / f"{name}.sql").read_text()
    assert "ServiceName != 'load-generator'" in sql
