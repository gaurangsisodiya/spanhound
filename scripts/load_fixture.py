"""Load one saved incident into the otel_test database, ready for an investigation.

Example:
  uv run python scripts/load_fixture.py paymentFailure
  SPANHOUND_CH_DATABASE=otel_test uv run python scripts/investigate.py \
    --alert "Customers report that checkout fails" \
    --start "2026-11-10 15:52:00" --end "2026-11-10 15:54:00"
"""

import argparse
import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

from spanhound.fixtures import TABLES, TEST_DB, admin_client, create_test_tables, fixture_loader

INCIDENTS_CSV = Path(__file__).resolve().parents[1] / "eval" / "incidents.csv"


def main() -> int:
    with INCIDENTS_CSV.open(newline="") as f:
        incidents = {row["name"]: row for row in csv.DictReader(f)}

    parser = argparse.ArgumentParser(description="Load a saved incident into otel_test.")
    parser.add_argument("incident", help=f"One of: {', '.join(incidents)}")
    args = parser.parse_args()

    incident = incidents.get(args.incident)
    if incident is None:
        print(f"Unknown incident {args.incident!r}. Choose from: {', '.join(incidents)}", file=sys.stderr)
        return 2

    load_dotenv()
    client = admin_client()
    try:
        missing = [t for t in TABLES if not client.command(f"EXISTS TABLE {TEST_DB}.{t}")]
        if missing:
            print(f"Creating {TEST_DB} tables")
            create_test_tables(client)
        fixture_loader(client)(args.incident)
        counts = {t: client.command(f"SELECT count() FROM {TEST_DB}.{t}") for t in TABLES}
    finally:
        client.close()

    print(f"Loaded {args.incident} into {TEST_DB}: "
          + ", ".join(f"{n:,} rows in {t}" for t, n in counts.items()))
    print(f"Window (UTC): {incident['start']} to {incident['end']}")
    print(f"Alert:        {incident['alert']}")
    print(f"Expected:     {incident['expected_service']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
