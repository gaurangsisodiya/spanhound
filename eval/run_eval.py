"""Offline evaluation: replay each saved incident through the agent and score the answers.

Example:
  uv run python eval/run_eval.py --runs 3
  uv run python eval/run_eval.py --runs 1 --only paymentFailure,baseline_1
"""

import argparse
import csv
import math
import os
import statistics
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import clickhouse_connect
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS_CSV = ROOT / "eval" / "incidents.csv"
RUNS_DIR = ROOT / "runs"

COLUMNS = [
    "incident", "run", "signal", "expected", "predicted", "correct", "grounded",
    "correct_and_grounded", "confidence", "tool_calls", "prompt_tokens",
    "completion_tokens", "seconds", "provider_retries", "stopped_by", "cause",
]


def read_incidents(only: str | None) -> list[dict]:
    with INCIDENTS_CSV.open(newline="") as f:
        incidents = list(csv.DictReader(f))
    if not only:
        return incidents
    wanted = [name.strip() for name in only.split(",") if name.strip()]
    known = {row["name"] for row in incidents}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        sys.exit(f"Unknown incident(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}")
    return [row for row in incidents if row["name"] in wanted]


def use_readonly_test_db() -> None:
    """Point the agent's tools at otel_test as the read-only user, and check it can read."""
    os.environ["SENTINEL_CH_DATABASE"] = "otel_test"
    os.environ["SENTINEL_CH_USER"] = "sentinel_ro"
    os.environ["SENTINEL_CH_PASSWORD"] = os.getenv("SENTINEL_RO_PASSWORD", "sentinel_ro_dev")
    try:
        client = clickhouse_connect.get_client(
            host=os.getenv("SENTINEL_CH_HOST", "localhost"),
            port=int(os.getenv("SENTINEL_CH_PORT", "8123")),
            username=os.environ["SENTINEL_CH_USER"],
            password=os.environ["SENTINEL_CH_PASSWORD"],
        )
        client.query("SELECT count() FROM otel_test.otel_traces")
    except Exception as exc:
        sys.exit(
            "The read-only user cannot read otel_test. Was ClickHouse restarted after "
            f"adding the otel_test grant to infra/clickhouse/sentinel_ro.xml?\n  {exc}"
        )


def git_commit() -> str:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    return f"{commit} (uncommitted changes)" if git("status", "--porcelain") else commit


def run_once(incident: dict, run: int, log_dir: Path) -> dict:
    from sentinel.agent import investigate

    expected = incident["expected_service"]
    try:
        result = investigate(
            incident["alert"],
            incident["start"],
            incident["end"],
            log_path=str(log_dir / f"{incident['name']}_{run}.jsonl"),
        )
    except Exception as exc:  # a crash in one run must not end the whole eval
        result = {
            "suspected_service": "error", "cause": f"{type(exc).__name__}: {exc}"[:300],
            "grounded": False, "confidence": 0, "tool_calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "seconds": 0, "provider_retries": 0, "stopped_by": "exception",
        }

    predicted = result["suspected_service"]
    correct = predicted == expected
    return {
        "incident": incident["name"],
        "run": run,
        "signal": incident["signal"],
        "expected": expected,
        "predicted": predicted,
        "correct": correct,
        "grounded": result["grounded"],
        "correct_and_grounded": correct and result["grounded"],
        "confidence": result["confidence"],
        "tool_calls": result["tool_calls"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "seconds": result["seconds"],
        "provider_retries": result["provider_retries"],
        "stopped_by": result["stopped_by"],
        "cause": result["cause"],
    }


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def rate(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.0%})" if total else "n/a (no runs)"


def summary_rows(rows: list[dict]) -> list[tuple[str, str]]:
    faults = [r for r in rows if r["expected"] != "none"]
    baselines = [r for r in rows if r["expected"] == "none"]
    seconds = [r["seconds"] for r in rows]
    tokens = [r["prompt_tokens"] + r["completion_tokens"] for r in rows]

    price_in, price_out = os.getenv("SENTINEL_PRICE_IN"), os.getenv("SENTINEL_PRICE_OUT")
    if price_in and price_out:
        costs = [
            (r["prompt_tokens"] * float(price_in) + r["completion_tokens"] * float(price_out)) / 1e6
            for r in rows
        ]
        cost = f"${statistics.mean(costs):.4f}"
    else:
        cost = "not set (SENTINEL_PRICE_IN / SENTINEL_PRICE_OUT)"

    return [
        ("Fault accuracy", rate(sum(r["correct"] for r in faults), len(faults))),
        ("False-alarm rate", rate(sum(r["predicted"] != "none" for r in baselines), len(baselines))),
        ("Correct and grounded", rate(sum(r["correct_and_grounded"] for r in rows), len(rows))),
        ("Mean seconds", f"{statistics.mean(seconds):.1f}"),
        ("p95 seconds", f"{p95(seconds):.1f}"),
        ("Mean tokens per run", f"{statistics.mean(tokens):,.0f}"),
        ("Mean tool calls", f"{statistics.mean(r['tool_calls'] for r in rows):.1f}"),
        ("Provider errors", str(sum(r["stopped_by"] in ("provider_error", "exception") for r in rows))),
        ("Estimated cost per run", cost),
    ]


def markdown_table(header: tuple[str, ...], rows: list[tuple]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def per_incident_rows(rows: list[dict], incidents: list[dict], runs: int) -> list[tuple]:
    table = []
    for incident in incidents:
        mine = [r for r in rows if r["incident"] == incident["name"]]
        seen = Counter(r["predicted"] for r in mine)
        predictions = ", ".join(f"{name} x{n}" for name, n in seen.most_common())
        table.append((
            incident["name"], incident["expected_service"],
            f"{sum(r['correct'] for r in mine)}/{runs}", predictions,
        ))
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline evaluation.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per incident (default 3)")
    parser.add_argument("--only", help="Comma-separated incident names")
    parser.add_argument("--out", default=str(ROOT / "results"), help="Output directory (default results/)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    incidents = read_incidents(args.only)
    use_readonly_test_db()

    from sentinel.fixtures import admin_client, create_test_tables, fixture_loader

    admin = admin_client()
    create_test_tables(admin)
    load = fixture_loader(admin)

    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    log_dir = RUNS_DIR / f"eval_{stamp}"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for incident in incidents:
        load(incident["name"])
        for run in range(1, args.runs + 1):
            row = run_once(incident, run, log_dir)
            rows.append(row)
            verdict = "correct" if row["correct"] else f"WRONG (expected {row['expected']})"
            print(
                f"{incident['name']} run {run}/{args.runs}: {row['predicted']} {verdict}, "
                f"{row['seconds']}s",
                flush=True,
            )

    csv_path = out_dir / f"eval_{stamp}.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    summary = markdown_table(("Metric", "Value"), summary_rows(rows))
    model = os.getenv("SENTINEL_MODEL", "openai/gpt-oss-120b")  # same default as agent.py
    temperature = os.getenv("SENTINEL_TEMPERATURE", "0")
    md = "\n\n".join([
        f"# Eval {stamp}",
        "## Summary",
        summary,
        "## Per incident",
        markdown_table(
            ("Incident", "Expected", "Correct", "Predictions"),
            per_incident_rows(rows, incidents, args.runs),
        ),
        "## Setup",
        "\n".join([
            f"- Model: {model}",
            f"- Temperature: {temperature}",
            f"- Runs per incident: {args.runs}",
            f"- Date (UTC): {datetime.now(UTC):%Y-%m-%d %H:%M}",
            f"- Commit: {git_commit()}",
            f"- Step logs: {log_dir.relative_to(ROOT)}",
        ]),
    ]) + "\n"
    md_path = out_dir / f"eval_{stamp}.md"
    md_path.write_text(md)

    print()
    print(summary)
    print(f"\nWrote {csv_path} and {md_path}")


if __name__ == "__main__":
    main()
