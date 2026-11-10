from sentinel.db import check_window, run_query


def error_origins(start: str, end: str, limit: int = 8) -> str:
    """Where errors started in a time window (UTC): the deepest failing spans."""
    check_window(start, end)
    rows = run_query("error_origins", start=start, end=end)
    if not rows:
        return f"No error origins between {start} and {end} UTC."

    lines = [f"Error origins {start} to {end} UTC, most frequent first:"]
    for service, span, kind, message, count in rows[:limit]:
        text = " ".join(message.split())[:160] or "(no status message)"
        note = ""
        if kind == "Client":
            note = " [client call that got no response: suspect the service it called]"
        lines.append(f"- {service} / {span} ({kind}) x{count}: {text}{note}")
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more origin patterns")
    return "\n".join(lines)


def slow_operations(start: str, end: str, limit: int = 5) -> str:
    """Slowest operations in a time window (UTC), ranked by p95 self time."""
    check_window(start, end)
    rows = run_query("slow_operations", start=start, end=end)
    if not rows:
        return f"No operations found between {start} and {end} UTC."

    lines = [f"Slowest operations {start} to {end} UTC, sorted by p95 self time:"]
    for service, span, kind, count, total_ms, self_ms in rows[:limit]:
        note = ""
        if kind == "Client":
            note = " [client call: this time was spent waiting on the called service]"
        unit = "span" if count == 1 else "spans"
        lines.append(
            f"- {service} / {span}: p95 self {self_ms:,.0f} ms "
            f"(total {total_ms:,.0f} ms) over {count} {unit}{note}"
        )
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more operations")
    return "\n".join(lines)