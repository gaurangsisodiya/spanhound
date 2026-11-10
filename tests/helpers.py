import re

# error_origins: "- <service> / <span name> (<kind>) x<count>: <text>"
ORIGIN_LINE = re.compile(r"^- (?P<service>.+?) / (?P<span>.*) \((?P<kind>\w+)\) x\d+:")
# slow_operations: "- <service> / <span name>: p95 self ..." (no kind; Client rows carry a note)
SLOW_LINE = re.compile(r"^- (?P<service>.+?) / (?P<span>.*?): p95 self ")
CLIENT_NOTE = "[client call:"


def first_result_line(output: str) -> str:
    """The first "- ..." line of a tool's output (the line after the header)."""
    for line in output.splitlines():
        if line.startswith("- "):
            return line
    raise AssertionError(f"no result lines in output:\n{output}")


def points_to(line: str, expected: str) -> bool:
    """True if a result line names `expected`, directly or as the target of a Client call."""
    if match := ORIGIN_LINE.match(line):
        kind = match["kind"]
    elif match := SLOW_LINE.match(line):
        kind = "Client" if CLIENT_NOTE in line else None
    else:
        raise AssertionError(f"not a result line: {line!r}")

    if match["service"] == expected:
        return True
    return kind == "Client" and expected.replace("-", "") in match["span"].lower()
