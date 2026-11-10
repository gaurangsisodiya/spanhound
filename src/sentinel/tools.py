import re

from sentinel.db import check_window, run_query

SLOW_MS = 1000  # p95 self time at or above this is marked [SLOW]

TRACE_FIELDS = (
    "span_id", "parent_id", "service", "name", "kind",
    "ms", "status", "message", "exceptions",
)


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

    lines = [
        f"Slowest operations {start} to {end} UTC, sorted by p95 self time; operations "
        f"at or above {SLOW_MS:,} ms p95 self time are marked [SLOW]:"
    ]
    any_slow = False
    for service, span, kind, count, total_ms, self_ms in rows[:limit]:
        slow = self_ms >= SLOW_MS
        any_slow = any_slow or slow
        marker = "[SLOW] " if slow else ""
        note = ""
        if slow and kind == "Client":
            note = " [client call: this time was spent waiting on the called service]"
        unit = "span" if count == 1 else "spans"
        lines.append(
            f"- {marker}{service} / {span}: p95 self {self_ms:,.0f} ms "
            f"(total {total_ms:,.0f} ms) over {count} {unit}{note}"
        )
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more operations")
    if not any_slow:
        lines.append("No operation is marked [SLOW].")
    return "\n".join(lines)


def error_logs(start: str, end: str, limit: int = 8) -> str:
    """Warning and error log patterns in a time window (UTC), each with an example trace."""
    check_window(start, end)
    rows = run_query("error_logs", start=start, end=end)
    if not rows:
        return f"No warning or error logs between {start} and {end} UTC."

    lines = [
        f"Logs {start} to {end} UTC: warning and error log patterns, "
        f"numbers masked as <N>, most frequent first:"
    ]
    for service, pattern, count, first_seen, example_trace in rows[:limit]:
        text = _clean(pattern)
        trace = f" [trace {example_trace}]" if example_trace else ""
        lines.append(
            f"- {service} x{count} (first {first_seen:%H:%M:%S}): {text}{trace}"
        )
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more patterns")
    return "\n".join(lines)


def get_trace(trace_id: str, max_lines: int = 40) -> str:
    """One trace as a span tree; if it has errors, only the paths leading to them."""
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ValueError(f"trace_id must be 32 lowercase hex characters, got {trace_id!r}")
    rows = run_query("trace", trace_id=trace_id)
    if not rows:
        return f"Trace {trace_id} not found."

    spans = [dict(zip(TRACE_FIELDS, row)) for row in rows]
    ids = {span["span_id"] for span in spans}
    roots = [s for s in spans if not s["parent_id"] or s["parent_id"] not in ids]
    children = _children_map(spans, ids)
    error_count = sum(1 for s in spans if s["status"] == "Error")

    keep = None
    if error_count:
        keep = set()
        for root in roots:
            _mark_error_paths(root, children, keep)

    body = []
    for root in roots:
        _walk(root, 0, children, keep, body)
    if len(body) > max_lines:
        body = body[:max_lines] + ["... output truncated"]

    lines = [f"Trace {trace_id}: {len(spans)} spans, {error_count} with errors"] + body
    if keep is not None:
        hidden = len(spans) - len(keep)
        lines.append(f"({hidden} healthy {'span' if hidden == 1 else 'spans'} hidden)")
    return "\n".join(lines)


def _clean(text: str) -> str:
    """Collapse runs of whitespace to one space and cut at 160 characters."""
    return " ".join(text.split())[:160]


def _children_map(spans: list[dict], ids: set[str]) -> dict[str, list[dict]]:
    """Map each span id to its child spans, keeping the time order rows arrived in."""
    children = {}
    for span in spans:
        if span["parent_id"] in ids:
            children.setdefault(span["parent_id"], []).append(span)
    return children


def _mark_error_paths(span: dict, children: dict, keep: set) -> bool:
    """Add this span to `keep` if it or anything below it is an error. Returns whether it was added."""
    below = [_mark_error_paths(c, children, keep) for c in children.get(span["span_id"], [])]
    if span["status"] == "Error" or any(below):
        keep.add(span["span_id"])
        return True
    return False


def _walk(span: dict, depth: int, children: dict, keep: set | None, out: list[str]) -> None:
    """Append one line for this span, then do the same for its children, one level deeper."""
    if keep is not None and span["span_id"] not in keep:
        return
    line = f"{'  ' * depth}{span['service']} / {span['name']} ({span['kind']}) {span['ms']} ms"
    if span["parent_id"] and depth == 0:
        line += " (parent not in trace)"
    if span["status"] == "Error":
        text = _error_text(span["message"], span["exceptions"])
        line += f" ERROR: {text}" if text else " ERROR"
    out.append(line)
    for child in children.get(span["span_id"], []):
        _walk(child, depth + 1, children, keep, out)


def _error_text(message: str, exceptions: str) -> str:
    """Status message plus exception text, skipping empties and duplicates."""
    message, exceptions = " ".join(message.split()), " ".join(exceptions.split())
    parts = [message] if message else []
    if exceptions and exceptions not in message:
        parts.append(exceptions)
    return _clean(" | ".join(parts))