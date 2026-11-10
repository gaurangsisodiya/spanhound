import pytest

from sentinel.db import check_window
from sentinel.tools import get_trace


@pytest.mark.parametrize(
    "start, end",
    [
        ("2026-11-10 15:00:00", "2026-11-10 15:00:00"),  # end equals start
        ("2026-11-10 15:00:00", "2026-11-10 14:59:00"),  # end before start
    ],
)
def test_check_window_rejects_end_not_after_start(start, end):
    with pytest.raises(ValueError):
        check_window(start, end)


def test_check_window_rejects_window_over_60_minutes():
    with pytest.raises(ValueError):
        check_window("2026-11-10 15:00:00", "2026-11-10 16:01:00")


@pytest.mark.parametrize(
    "trace_id",
    [
        "",
        "abc123",                              # too short
        "FE9116C418CC72E14AD89C47BFD92F1D",    # uppercase
        "fe9116c418cc72e14ad89c47bfd92f1",     # 31 characters
        "fe9116c418cc72e14ad89c47bfd92f1dd",   # 33 characters
        "ge9116c418cc72e14ad89c47bfd92f1d",    # not hex
        "fe9116c418cc72e14ad89c47bfd92f1d\n",  # trailing newline
        "' OR 1=1 --",
    ],
)
def test_get_trace_rejects_bad_ids(trace_id):
    with pytest.raises(ValueError):
        get_trace(trace_id)
