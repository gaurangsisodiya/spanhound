"""Resume logic of eval/run_eval.py: no database, no network."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "eval" / "run_eval.py"
spec = importlib.util.spec_from_file_location("run_eval", SCRIPT)
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)

INCIDENTS = [
    {"name": "paymentFailure", "expected_service": "payment"},
    {"name": "baseline_1", "expected_service": "none"},
]


def row(incident: str, run: int, **overrides) -> dict:
    base = {
        "incident": incident, "run": run, "signal": "errors", "expected": "payment",
        "predicted": "payment", "correct": True, "grounded": True,
        "correct_and_grounded": True, "confidence": 0.9, "tool_calls": 2,
        "prompt_tokens": 3000, "completion_tokens": 1500, "seconds": 21.5,
        "provider_retries": 0, "stopped_by": "answer", "cause": "payment, rejected charges",
    }
    return {**base, **overrides}


def test_remaining_runs_skips_recorded_pairs():
    done = [row("paymentFailure", 1), row("paymentFailure", 3), row("baseline_1", 2)]

    todo = run_eval.remaining_runs(INCIDENTS, 3, done)

    assert [(i["name"], r) for i, r in todo] == [
        ("paymentFailure", 2), ("baseline_1", 1), ("baseline_1", 3),
    ]


def test_remaining_runs_accepts_run_numbers_read_as_text():
    done = [row("paymentFailure", "1")]  # straight from csv.DictReader

    todo = run_eval.remaining_runs(INCIDENTS[:1], 2, done)

    assert [(i["name"], r) for i, r in todo] == [("paymentFailure", 2)]


def test_nothing_remaining_when_all_done():
    done = [row(i["name"], r) for i in INCIDENTS for r in (1, 2)]

    assert run_eval.remaining_runs(INCIDENTS, 2, done) == []


def test_append_then_load_round_trip(tmp_path):
    path = tmp_path / "eval_test.csv"
    run_eval.append_row(path, row("paymentFailure", 1))
    run_eval.append_row(path, row("paymentFailure", 2, correct=False, predicted="checkout", seconds=30.0))

    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(run_eval.COLUMNS)  # header written once
    assert len(lines) == 3

    rows = run_eval.load_rows(path)
    assert [(r["run"], r["correct"], r["seconds"]) for r in rows] == [(1, True, 21.5), (2, False, 30.0)]
    assert rows[0]["cause"] == "payment, rejected charges"  # comma survives CSV quoting
    assert run_eval.remaining_runs(INCIDENTS[:1], 2, rows) == []


def test_load_rows_rejects_wrong_columns(tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("a,b\n1,2\n")

    with pytest.raises(SystemExit):
        run_eval.load_rows(path)


def test_ungrounded_quotes_round_trip(tmp_path):
    path = tmp_path / "eval_test.csv"
    quotes = ["inventory-service: timeout", 'a "quoted", text']
    run_eval.append_row(path, row("paymentFailure", 1, ungrounded_quotes=run_eval.json.dumps(quotes)))

    rows = run_eval.load_rows(path)

    assert rows[0]["ungrounded_quotes"] == quotes
    assert "inventory-service: timeout" in run_eval.ungrounded_section(rows)
