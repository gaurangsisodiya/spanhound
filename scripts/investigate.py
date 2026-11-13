"""Run the investigator agent on one alert.

Example:
  uv run python scripts/investigate.py --alert "Checkout fails" \
      --start "2026-11-10 15:52:00" --end "2026-11-10 15:54:00"
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from spanhound.agent import investigate

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


def print_step(step: dict) -> None:
    print(f"[{step['step']}] {step['tool']} {step['arguments']}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Investigate an alert with the spanhound agent.")
    parser.add_argument("--alert", required=True, help="What was reported")
    parser.add_argument("--start", required=True, help="Window start, UTC, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="Window end, UTC, YYYY-MM-DD HH:MM:SS")
    args = parser.parse_args()

    RUNS_DIR.mkdir(exist_ok=True)
    log_path = RUNS_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
    print(f"Step log: {log_path}")

    result = investigate(
        args.alert, args.start, args.end, log_path=str(log_path), on_step=print_step
    )
    if result["stopped_by"] == "provider_error":
        print(f"Investigation failed, model provider error: {result['cause']}", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 1 if result["stopped_by"] == "provider_error" else 0


if __name__ == "__main__":
    sys.exit(main())
