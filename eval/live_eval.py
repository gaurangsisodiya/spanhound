"""Live evaluation: inject failures into the running demo shop with flagd, then investigate.

Each run switches one feature flag on for a few minutes (or nothing, for a control run),
switches it off, waits for spans to be exported, and asks the agent what broke.

Examples:
  uv run python eval/live_eval.py --dry-run
  uv run python eval/live_eval.py
  uv run python eval/live_eval.py --resume results/live_eval_<timestamp>.csv
  uv run python eval/live_eval.py --smoke intlShippingSlowdown
"""

import argparse
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import (  # noqa: E402  (needs the sys.path line above)
    COLUMNS as EVAL_COLUMNS,
    ROOT,
    RUNS_DIR,
    append_row,
    git_commit,
    load_rows,
    markdown_table,
    read_incidents,
    safe_investigate,
    scored_fields,
    summary_rows,
    ungrounded_section,
)

FLAGS = (
    "paymentFailure", "cartFailure", "productCatalogFailure",
    "adFailure", "paymentUnreachable", "intlShippingSlowdown",
)
CONTROL = "control"
CONTROL_ALERT = "Routine check: is anything broken?"
RUNS_PER_FLAG = 3
CONTROL_RUNS = 6
EXPORT_WAIT_SECONDS = 90
ON_MINUTES_OVERRIDE = {"cartFailure": 4}
INVESTIGATION_ESTIMATE_MINUTES = 1  # for the --dry-run duration estimate only

DEFAULT_FLAGS_FILE = Path.home() / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
DEFAULT_LOADGEN = "http://localhost:8080/loadgen"
DEFAULT_SHOP = "http://localhost:8080"
SMOKE_ON_MINUTES = 2

COLUMNS = ["flag", "variant", "start", "end", "fault_observed"] + EVAL_COLUMNS

# ---------------------------------------------------------------- international orders
# intlShippingSlowdown only delays ship-order for non-US addresses, and only 1 of the
# load generator's 9 people is outside the US, so some windows would hold no such order.
# During that flag's window the eval places its own orders through the shop's HTTP API,
# with the same requests the load generator sends (src/load-generator/locustfile.py ->
# src/frontend/pages/api/cart.ts and checkout.ts).

INTL_ORDERS = 3
INTL_PRODUCT = "66VCHSJNUP"  # any catalog product except OLJCESPC7Z (productCatalogFailure's)
INTL_PERSON = {  # the Canadian person from src/load-generator/people.json
    "email": "tobias@example.com",
    "address": {
        "streetAddress": "150 Elgin St",
        "zipCode": "K2P1L4",
        "city": "Ottawa",
        "state": "ON",
        "country": "Canada",
    },
    "userCurrency": "CAD",
    "creditCard": {
        "creditCardNumber": "4763-1844-9699-8031",
        "creditCardExpirationMonth": 7,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 488,
    },
}


def intl_order_payloads(user_id: str) -> tuple[dict, dict]:
    """(add-to-cart body, checkout body) for one international order by `user_id`."""
    cart = {"userId": user_id, "item": {"productId": INTL_PRODUCT, "quantity": 1}}
    checkout = {**json.loads(json.dumps(INTL_PERSON)), "userId": user_id}
    return cart, checkout


def order_offsets(window_seconds: float, n: int = INTL_ORDERS) -> list[float]:
    """Seconds into the window at which to start each order: evenly spaced, away from the edges."""
    return [window_seconds * (i + 0.5) / (n + 0.5) for i in range(n)]


def post_json(url: str, body: dict, timeout: float = 60) -> int:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def place_intl_order(shop: str) -> str:
    """Add an item to a fresh cart, then check out to Canada. Returns a status summary."""
    cart, checkout = intl_order_payloads(str(uuid.uuid4()))
    try:
        cart_status = post_json(f"{shop}/api/cart", cart)
        checkout_status = post_json(f"{shop}/api/checkout?currencyCode=CAD", checkout)
    except Exception as exc:
        return f"failed: {type(exc).__name__}: {exc}"
    return f"cart HTTP {cart_status}, checkout HTTP {checkout_status}"


def hold_window(name: str, seconds: float, shop: str) -> None:
    """Keep the fault on for `seconds`; for intlShippingSlowdown, place orders across it."""
    if name != "intlShippingSlowdown":
        wait(seconds, "control" if name == CONTROL else "fault on")
        return
    print(f"  fault on for {seconds / 60:.1f} min, placing {INTL_ORDERS} orders to Canada", flush=True)
    began = time.monotonic()
    for i, offset in enumerate(order_offsets(seconds), 1):
        time.sleep(max(0.0, offset - (time.monotonic() - began)))
        print(f"  international order {i}/{INTL_ORDERS}: {place_intl_order(shop)}", flush=True)
    time.sleep(max(0.0, seconds - (time.monotonic() - began)))


# ---------------------------------------------------------------- fault presence
# Did the injected fault actually show up in the window? Checked with the admin client,
# read-only queries. A run where it did not is not the agent's fault.

_ERROR_IN = "ServiceName = {service:String} AND StatusCode = 'Error'"
FAULT_CHECKS: dict[str, tuple[str, dict]] = {
    "paymentFailure": (_ERROR_IN, {"service": "payment"}),
    "cartFailure": (_ERROR_IN, {"service": "cart"}),
    "productCatalogFailure": (_ERROR_IN, {"service": "product-catalog"}),
    "adFailure": (_ERROR_IN, {"service": "ad"}),
    "paymentUnreachable": (
        "ServiceName = 'checkout' AND SpanKind = 'Client' AND StatusCode = 'Error'"
        " AND SpanName LIKE '%PaymentService%'",
        {},
    ),
    "intlShippingSlowdown": ("ServiceName = 'shipping' AND Duration > 5000000000", {}),
}


def fault_query(name: str, start: str, end: str) -> tuple[str, dict] | None:
    """(SQL, parameters) counting the spans that prove `name`'s fault happened; None for controls."""
    if name == CONTROL:
        return None
    condition, params = FAULT_CHECKS[name]
    sql = (
        "SELECT count() FROM otel.otel_traces"
        " WHERE Timestamp >= {start:DateTime} AND Timestamp < {end:DateTime}"
        f" AND {condition}"
    )
    return sql, {**params, "start": start, "end": end}


def fault_observed(name: str, start: str, end: str) -> bool | None:
    """True if the fault's spans are there, False if not, None if the check itself failed."""
    query = fault_query(name, start, end)
    if query is None:
        return True
    from sentinel.fixtures import admin_client

    try:
        client = admin_client()
        try:
            count = client.query(query[0], parameters=query[1]).result_rows[0][0]
        finally:
            client.close()
    except Exception as exc:
        print(f"  WARNING: fault-presence check failed: {exc}", file=sys.stderr, flush=True)
        return None
    print(f"  fault-presence check: {count} matching spans", flush=True)
    return count > 0


# ---------------------------------------------------------------- flags file

def read_flags(path: Path) -> dict:
    return json.loads(path.read_text())


def write_flags(path: Path, data: dict) -> None:
    """Write-then-rename, like flagd-ui, so flagd never reads a half-written file."""
    tmp = path.with_name(path.name + ".sentinel.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, path.stat().st_mode)
    os.replace(tmp, path)


def _targeting_if(flag: dict) -> list | None:
    """The [condition, then, else] list of a flag with an if-targeting rule, else None."""
    rule = flag.get("targeting", {}).get("if")
    return rule if isinstance(rule, list) and len(rule) == 3 else None


def strongest_variant(flag: dict) -> str:
    """'on' for on/off flags, otherwise the variant with the largest value (100%, 10sec)."""
    variants = flag["variants"]
    if "on" in variants:
        return "on"
    return max((name for name in variants if name != "off"), key=lambda name: variants[name])


def current_variant(flag: dict) -> str:
    """The variant the flag is switched to, read the same place set_variant writes it."""
    rule = _targeting_if(flag)
    return rule[1] if rule else flag["defaultVariant"]


def set_variant(path: Path, flag_name: str, variant: str) -> None:
    """Switch one flag to `variant`, leaving every other flag untouched.

    Like flagd-ui: a flag with an if-targeting rule gets the variant in the rule's
    "then" branch (productCatalogFailure fails only product OLJCESPC7Z); any other
    flag gets it as defaultVariant.
    """
    data = read_flags(path)
    flag = data["flags"][flag_name]
    if variant not in flag["variants"]:
        raise ValueError(f"{flag_name} has no variant {variant!r}")
    rule = _targeting_if(flag)
    if rule:
        rule[1] = variant
    else:
        flag["defaultVariant"] = variant
    write_flags(path, data)


def flags_not_off(path: Path) -> list[str]:
    """Flags in the file (all of them, not only ours) that are not switched off."""
    flags = read_flags(path)["flags"]
    return [name for name, flag in flags.items()
            if current_variant(flag) != "off" or flag["defaultVariant"] != "off"]


def all_off(path: Path) -> None:
    """Switch every flag this eval uses back off. Safe to call more than once."""
    for name in FLAGS:
        try:
            if current_variant(read_flags(path)["flags"][name]) != "off":
                set_variant(path, name, "off")
                print(f"  {name} switched off", flush=True)
        except Exception as exc:  # keep going: the other flags must still be reset
            print(f"  WARNING: could not switch {name} off: {exc}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- schedule

def build_schedule(seed: int) -> list[tuple[str, int]]:
    """(flag or 'control', run number) for every run, shuffled with a fixed seed."""
    runs = [(flag, n) for flag in FLAGS for n in range(1, RUNS_PER_FLAG + 1)]
    runs += [(CONTROL, n) for n in range(1, CONTROL_RUNS + 1)]
    random.Random(seed).shuffle(runs)
    return runs


def on_minutes_for(name: str, on_minutes: float) -> float:
    return ON_MINUTES_OVERRIDE.get(name, on_minutes)


def run_minutes(name: str, on_minutes: float, cooldown_minutes: float) -> float:
    return (on_minutes_for(name, on_minutes) + EXPORT_WAIT_SECONDS / 60
            + INVESTIGATION_ESTIMATE_MINUTES + cooldown_minutes)


def remaining(schedule: list[tuple[str, int]], done_rows: list[dict]) -> list[tuple[str, int]]:
    done = {(row["incident"], int(row["run"])) for row in done_rows}
    return [item for item in schedule if item not in done]


# ---------------------------------------------------------------- load generator

def loadgen_state(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/stats/requests", timeout=10) as response:
        return json.load(response)


def ensure_loadgen(base: str, users: int) -> None:
    """Start Locust (or reset its user count) unless it is already running with `users`."""
    try:
        state = loadgen_state(base)
    except Exception as exc:
        sys.exit(f"Cannot reach the load generator at {base}/ . Is the demo running?\n  {exc}")
    if state.get("state") in ("running", "spawning") and state.get("user_count") == users:
        print(f"Load generator running with {users} users.")
        return

    print(f"Load generator is {state.get('state')} with {state.get('user_count')} users; "
          f"starting it with {users}.")
    form = urllib.parse.urlencode({"user_count": users, "spawn_rate": users}).encode()
    with urllib.request.urlopen(f"{base}/swarm", data=form, timeout=10):
        pass
    for _ in range(30):
        time.sleep(2)
        state = loadgen_state(base)
        if state.get("state") == "running" and state.get("user_count") == users:
            print(f"Load generator running with {users} users.")
            return
    sys.exit(f"Load generator did not reach {users} running users: {state.get('state')}")


# ---------------------------------------------------------------- running

def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def wait(seconds: float, why: str) -> None:
    print(f"  waiting {seconds / 60:.1f} min ({why})", flush=True)
    time.sleep(seconds)


def run_one(name: str, run: int, args, incidents: dict, log_dir: Path,
            on_minutes: float) -> tuple[dict, dict]:
    """One injected (or control) run. Returns (CSV row, the agent's full result)."""
    is_control = name == CONTROL
    if is_control:
        variant, alert, expected, signal_kind = "", CONTROL_ALERT, "none", "none"
    else:
        flag = read_flags(args.flags_file)["flags"][name]
        variant = strongest_variant(flag)
        alert = incidents[name]["alert"]
        expected = incidents[name]["expected_service"]
        signal_kind = incidents[name]["signal"]

    start = utc_now()
    if not is_control:
        set_variant(args.flags_file, name, variant)
        print(f"  {name} -> {variant}", flush=True)
    try:
        hold_window(name, on_minutes * 60, args.shop)
    finally:
        if not is_control:
            set_variant(args.flags_file, name, "off")
            print(f"  {name} -> off", flush=True)
    end = utc_now()

    wait(EXPORT_WAIT_SECONDS, "span export")
    observed = fault_observed(name, fmt(start), fmt(end))
    result = safe_investigate(alert, fmt(start), fmt(end), log_dir / f"{name}_{run}.jsonl")
    row = {
        "flag": "" if is_control else name,
        "variant": variant,
        "start": fmt(start),
        "end": fmt(end),
        "fault_observed": observed,
        "incident": name,
        "run": run,
        "signal": signal_kind,
        **scored_fields(result, expected),
    }
    return row, result


def use_readonly_live_db() -> None:
    os.environ["SENTINEL_CH_DATABASE"] = "otel"
    os.environ["SENTINEL_CH_USER"] = "sentinel_ro"
    os.environ["SENTINEL_CH_PASSWORD"] = os.getenv("SENTINEL_RO_PASSWORD", "sentinel_ro_dev")


# ---------------------------------------------------------------- report

def load_live_rows(path: Path) -> list[dict]:
    rows = load_rows(path, COLUMNS)
    for row in rows:
        row["fault_observed"] = {"True": True, "False": False}.get(row["fault_observed"])
    return rows


def observed_accuracy_row(rows: list[dict]) -> tuple[str, str]:
    observed = [r for r in rows if r["expected"] != "none" and r["fault_observed"] is True]
    hits = sum(r["correct"] for r in observed)
    value = f"{hits}/{len(observed)} ({hits / len(observed):.0%})" if observed else "n/a (no runs)"
    return ("Fault accuracy, fault observed", value)


def unobserved_section(rows: list[dict]) -> str:
    """Fault runs where the fault-presence check did not find the fault (or could not run)."""
    lines = [
        f"- {r['incident']} run {r['run']} ({r['start']} to {r['end']}): "
        f"{'not observed' if r['fault_observed'] is False else 'check failed'}, "
        f"predicted {r['predicted']}"
        for r in rows
        if r["expected"] != "none" and r["fault_observed"] is not True
    ]
    return "\n".join(lines) or "None: the fault was observed in every fault run."


def per_flag_rows(rows: list[dict]) -> list[tuple]:
    table = []
    for name in (*FLAGS, CONTROL):
        mine = [r for r in rows if r["incident"] == name]
        if not mine:
            continue
        seen = Counter(r["predicted"] for r in mine)
        table.append((
            name,
            mine[0]["variant"] or "-",
            mine[0]["expected"],
            f"{sum(r['correct'] for r in mine)}/{len(mine)}",
            ", ".join(f"{p} x{n}" for p, n in seen.most_common()),
        ))
    return table


def write_markdown(csv_path: Path, args, log_dir: Path) -> tuple[Path, str]:
    rows = load_live_rows(csv_path)
    metrics = summary_rows(rows)
    metrics.insert(2, observed_accuracy_row(rows))  # after the two overall fault-accuracy rows
    summary = markdown_table(("Metric", "Value"), metrics)
    md = "\n\n".join([
        f"# {csv_path.stem.replace('_', ' ').capitalize()}",
        "## Summary",
        summary,
        "## Per flag",
        markdown_table(("Flag", "Variant", "Expected", "Correct", "Predictions"), per_flag_rows(rows)),
        "## Faults not observed",
        unobserved_section(rows),
        "## Ungrounded quotes",
        ungrounded_section(rows),
        "## Setup",
        "\n".join([
            f"- Model: {os.getenv('SENTINEL_MODEL', 'openai/gpt-oss-120b')}",
            f"- Temperature: {os.getenv('SENTINEL_TEMPERATURE', '0')}",
            f"- Runs: {RUNS_PER_FLAG} per flag, {CONTROL_RUNS} controls; seed {args.seed}",
            f"- Fault on: {args.on_minutes} min "
            + ", ".join(f"({k}: {v} min)" for k, v in ON_MINUTES_OVERRIDE.items()),
            f"- Export wait: {EXPORT_WAIT_SECONDS} s; cooldown: {args.cooldown_minutes} min",
            f"- Load generator users: {args.users}; "
            f"{INTL_ORDERS} extra orders to Canada during intlShippingSlowdown",
            f"- Date (UTC): {datetime.now(UTC):%Y-%m-%d %H:%M}",
            f"- Commit: {git_commit()}",
            f"- Step logs: {log_dir.relative_to(ROOT)}",
        ]),
    ]) + "\n"
    md_path = csv_path.with_suffix(".md")
    md_path.write_text(md)
    return md_path, summary


# ---------------------------------------------------------------- main

def print_schedule(todo: list[tuple[str, int]], args) -> None:
    clock = datetime.now(UTC)
    for i, (name, run) in enumerate(todo, 1):
        minutes = run_minutes(name, args.on_minutes, args.cooldown_minutes)
        on = on_minutes_for(name, args.on_minutes)
        print(f"{i:2}. {clock:%H:%M} UTC  {name} run {run}  (on {on:g} min, about {minutes:.1f} min total)")
        clock += timedelta(minutes=minutes)
    total = sum(run_minutes(n, args.on_minutes, args.cooldown_minutes) for n, _ in todo)
    print(f"\n{len(todo)} runs, about {total / 60:.1f} hours "
          f"(assuming {INVESTIGATION_ESTIMATE_MINUTES} min per investigation); "
          f"would finish around {clock:%H:%M} UTC if started now.")


def _interrupt(signum, frame):
    """A plain `kill` (SIGTERM) resets the flags the same way Ctrl+C does."""
    raise KeyboardInterrupt


def prepare(args) -> None:
    """Checks before touching anything: flags all off, load generator running."""
    if not args.flags_file.exists():
        sys.exit(f"flags file not found: {args.flags_file}")
    switched_on = flags_not_off(args.flags_file)
    if switched_on:
        sys.exit(f"These flags are not off; switch them off first: {', '.join(switched_on)}")
    ensure_loadgen(args.loadgen, args.users)
    use_readonly_live_db()


def smoke(args, incidents: dict) -> None:
    """One short run of one flag: print the fault check and the agent's report, no CSV."""
    minutes = SMOKE_ON_MINUTES + EXPORT_WAIT_SECONDS / 60 + INVESTIGATION_ESTIMATE_MINUTES
    print(f"Smoke run: {args.smoke} on for {SMOKE_ON_MINUTES} min, about {minutes:.1f} min in all.")
    if args.dry_run:
        return
    prepare(args)
    log_dir = RUNS_DIR / f"live_smoke_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    log_dir.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        row, result = run_one(args.smoke, 1, args, incidents, log_dir, SMOKE_ON_MINUTES)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return
    finally:
        all_off(args.flags_file)
    print(f"\nWindow {row['start']} to {row['end']} UTC")
    print(f"Fault observed: {row['fault_observed']}")
    print(f"Prediction: {row['predicted']} ({'correct' if row['correct'] else 'expected ' + row['expected']})")
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject failures into the live demo and investigate them.")
    parser.add_argument("--seed", type=int, default=42, help="Shuffle seed (default 42)")
    parser.add_argument("--users", type=int, default=5, help="Load generator users (default 5)")
    parser.add_argument("--on-minutes", type=float, default=3, help="Minutes each flag stays on (default 3; cartFailure 4)")
    parser.add_argument("--cooldown-minutes", type=float, default=2, help="Minutes between runs (default 2)")
    parser.add_argument("--flags-file", type=Path, default=DEFAULT_FLAGS_FILE, help="flagd's JSON file on the host")
    parser.add_argument("--loadgen", default=DEFAULT_LOADGEN, help="Locust base URL")
    parser.add_argument("--shop", default=DEFAULT_SHOP, help="Shop base URL for the extra orders")
    parser.add_argument("--smoke", choices=FLAGS, metavar="FLAG",
                        help=f"One {SMOKE_ON_MINUTES}-minute run of FLAG; prints the result, writes no CSV")
    parser.add_argument("--out", default=str(ROOT / "results"), help="Output directory (default results/)")
    parser.add_argument("--resume", help="Existing live results CSV to continue")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule; touch nothing")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    incidents = {row["name"]: row for row in read_incidents(None)}
    missing = [f for f in FLAGS if f not in incidents]
    if missing:
        sys.exit(f"eval/incidents.csv has no alert for: {', '.join(missing)}")

    if args.smoke:
        smoke(args, incidents)
        return

    if args.resume:
        csv_path = Path(args.resume)
        if not csv_path.exists():
            sys.exit(f"--resume: {csv_path} does not exist")
    else:
        csv_path = Path(args.out) / f"live_eval_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    log_dir = RUNS_DIR / csv_path.stem

    schedule = build_schedule(args.seed)
    todo = remaining(schedule, load_live_rows(csv_path))
    print(f"{len(schedule) - len(todo)} of {len(schedule)} runs already done, {len(todo)} remaining.")

    if args.dry_run:
        print_schedule(todo, args)
        return
    if not todo:
        md_path, summary = write_markdown(csv_path, args, log_dir)
        print(summary)
        return

    prepare(args)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results: {csv_path}")

    signal.signal(signal.SIGTERM, _interrupt)
    try:
        for i, (name, run) in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {utc_now():%H:%M:%S} {name} run {run}", flush=True)
            row, _ = run_one(name, run, args, incidents, log_dir,
                             on_minutes_for(name, args.on_minutes))
            append_row(csv_path, row, COLUMNS)
            verdict = "correct" if row["correct"] else f"WRONG (expected {row['expected']})"
            print(f"  {row['start']} to {row['end']}: {row['predicted']} {verdict}, "
                  f"{row['seconds']}s", flush=True)
            if i < len(todo):
                wait(args.cooldown_minutes * 60, "cooldown")
    except KeyboardInterrupt:
        print("\nInterrupted. Rows written so far are kept; continue with --resume.", flush=True)
    finally:
        print("Switching all eval flags off.", flush=True)
        all_off(args.flags_file)

    if load_rows(csv_path, COLUMNS):
        md_path, summary = write_markdown(csv_path, args, log_dir)
        print()
        print(summary)
        print(f"\nWrote {csv_path} and {md_path}")


if __name__ == "__main__":
    main()
