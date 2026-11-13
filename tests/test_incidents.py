import csv
from pathlib import Path

import pytest

from helpers import first_result_line, points_to
from spanhound.tools import error_logs, error_origins, get_trace, slow_operations

INCIDENTS_CSV = Path(__file__).resolve().parents[1] / "eval" / "incidents.csv"

with INCIDENTS_CSV.open(newline="") as f:
    INCIDENTS = list(csv.DictReader(f))


def _window(name: str) -> tuple[str, str]:
    row = next(r for r in INCIDENTS if r["name"] == name)
    return row["start"], row["end"]


@pytest.mark.parametrize("incident", INCIDENTS, ids=[r["name"] for r in INCIDENTS])
def test_incident(incident, load_incident):
    load_incident(incident["name"])
    start, end, expected = incident["start"], incident["end"], incident["expected_service"]

    match incident["signal"]:
        case "errors":
            output = error_origins(start, end)
            assert points_to(first_result_line(output), expected), output
        case "latency":
            output = slow_operations(start, end)
            assert points_to(first_result_line(output), expected), output
        case "none":
            output = error_origins(start, end)
            assert output.startswith("No error origins"), output
        case other:
            pytest.fail(f"unknown signal {other!r} for {incident['name']}")


def test_get_trace_shows_catalog_failure(load_incident):
    load_incident("productCatalogFailure")
    output = get_trace("fe9116c418cc72e14ad89c47bfd92f1d")
    assert "product-catalog" in output, output
    assert "Fail Feature Flag" in output, output


def test_error_logs_shows_payment_failure(load_incident):
    load_incident("paymentFailure")
    output = error_logs(*_window("paymentFailure"))
    assert "payment" in output, output
    assert "Invalid token" in output, output
