"""slow_operations formatting: run_query is faked, so no database is needed."""

import pytest

from spanhound import tools

WINDOW = ("2026-11-10 15:46:00", "2026-11-10 15:52:00")
CLIENT_NOTE = "[client call:"


@pytest.fixture
def rows(monkeypatch):
    """Set the rows the fake run_query returns: (service, span, kind, count, total_ms, self_ms)."""
    data = []
    monkeypatch.setattr(tools, "run_query", lambda name, **params: data)
    return data


def result_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("- ")]


def test_999_ms_is_not_slow(rows):
    rows.append(("checkout", "PlaceOrder", "Server", 4, 1200.0, 999.0))

    output = tools.slow_operations(*WINDOW)

    assert result_lines(output) == ["- checkout / PlaceOrder: p95 self 999 ms (total 1,200 ms) over 4 spans"]
    assert output.endswith("No operation is marked [SLOW].")


def test_1000_ms_is_slow(rows):
    rows.append(("shipping", "GetQuote", "Server", 1, 1000.0, 1000.0))

    output = tools.slow_operations(*WINDOW)

    assert result_lines(output) == ["- [SLOW] shipping / GetQuote: p95 self 1,000 ms (total 1,000 ms) over 1 span"]
    assert "No operation is marked [SLOW]." not in output


def test_client_note_only_on_slow_lines(rows):
    rows.extend([
        ("checkout", "oteldemo.ShippingService/GetQuote", "Client", 3, 9500.0, 9400.0),
        ("frontend", "oteldemo.CartService/GetCart", "Client", 8, 400.0, 380.0),
    ])

    slow, fast = result_lines(tools.slow_operations(*WINDOW))

    assert slow.startswith("- [SLOW] checkout / ") and slow.endswith(
        "[client call: this time was spent waiting on the called service]"
    )
    assert "[SLOW]" not in fast and CLIENT_NOTE not in fast


def test_header_names_threshold(rows):
    rows.append(("cart", "GetCart", "Server", 2, 50.0, 40.0))

    header = tools.slow_operations(*WINDOW).splitlines()[0]

    assert "at or above 1,000 ms p95 self time are marked [SLOW]" in header
