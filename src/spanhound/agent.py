"""The investigator agent: an LLM that calls the tools in spanhound.tools and reports a culprit."""

import json
import os
import re
import time
from collections.abc import Callable

import openai

from spanhound import tools

MAX_RESULT_CHARS = 4000
REPORT_KEYS = ("suspected_service", "cause", "evidence", "confidence")
REPEAT_PREFIX = "(Repeat call, same result as before.)"
HARMONY_RETRIES = 2
HARMONY_RETRY_TEMPERATURE = 0.3

KNOWN_SERVICES = (
    "ad", "cart", "checkout", "currency", "email", "flagd", "frontend",
    "frontend-proxy", "image-provider", "payment", "product-catalog", "quote",
    "recommendation", "shipping",
)

_TIME = {"type": "string", "description": "UTC time, format YYYY-MM-DD HH:MM:SS"}
_WINDOW = {
    "type": "object",
    "properties": {"start": _TIME, "end": _TIME},
    "required": ["start", "end"],
}


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


TOOLS = [
    _tool(
        "error_origins",
        "Where errors started in a UTC window: the deepest failing spans, most frequent "
        "first. Strongest error signal.",
        _WINDOW,
    ),
    _tool(
        "slow_operations",
        "Operations ranked by p95 self time (own time, excluding child calls) in a UTC "
        "window. Finds slowness.",
        _WINDOW,
    ),
    _tool(
        "error_logs",
        "Warning and error log patterns in a UTC window, with an example trace id per "
        "pattern. Supporting evidence only.",
        _WINDOW,
    ),
    _tool(
        "get_trace",
        "One trace as a span tree showing only the paths to errors. trace_id is 32 "
        "lowercase hex characters.",
        {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "32 lowercase hex characters"}
            },
            "required": ["trace_id"],
        },
    ),
]

TOOL_FUNCTIONS: dict[str, Callable[..., str]] = {
    "error_origins": tools.error_origins,
    "slow_operations": tools.slow_operations,
    "error_logs": tools.error_logs,
    "get_trace": tools.get_trace,
}

SYSTEM_PROMPT = f"""\
You investigate incidents in an online shop made of microservices, using
OpenTelemetry traces and logs. You get an alert and a UTC time window. Find the
service that caused the problem, using the tools.

The services are: {", ".join(KNOWN_SERVICES)}.

Call one tool at a time and wait for its result. Never write tool results
yourself. Only quote text that a tool actually returned.

How to read the evidence:
1. error_origins is the strongest error signal. The service of a Server or
   Internal origin is the prime suspect.
2. An origin that is a Client span which got no response means the CALLED
   service is at fault (it is named in the span name), not the caller.
3. frontend-proxy origins with no error message are the proxy timing out.
   They are symptoms, not causes.
4. Errors mentioning "deadline exceeded", "context canceled" or "timeout" mean
   something was too slow. Use slow_operations to find what.
5. slow_operations ranks each operation's own time. Lines marked as client calls
   are time spent waiting on the called service, so point at that service.
6. error_logs is supporting evidence only. Callers often log their callee's
   failure. Use its trace ids with get_trace to confirm the real origin.
7. Only operations marked [SLOW] count as slowness. If error_origins finds no
   errors and slow_operations marks nothing [SLOW], the answer is none, even if
   some operations take a few hundred milliseconds. Never blame a service
   without an error origin or a [SLOW] operation.
8. Use the alert's time window unless you have a reason to narrow it.

When you are done, reply with ONLY a JSON object, no other text.
suspected_service must be exactly one of the services listed above, or "none".
{{"suspected_service": "<one of the services listed above, or none>",
 "cause": "<one sentence>",
 "evidence": [{{"tool": "<tool name>", "quote": "<exact text from that tool's output>"}}],
 "confidence": <number from 0 to 1>}}
"""

BUDGET_MESSAGE = "Tool budget used up. Give your final JSON report now."
RETRY_MESSAGE = "Reply with only the JSON object."


def default_client():
    from dotenv import load_dotenv

    load_dotenv()
    return openai.OpenAI(
        base_url="https://api.together.xyz/v1",
        api_key=os.environ["TOGETHER_API_KEY"],
        timeout=120,
        max_retries=2,
    )


def run_tool(name: str, raw_args: str) -> str:
    """Run one tool call. Any failure becomes a 'Tool error:' result for the model to read."""
    try:
        function = TOOL_FUNCTIONS[name]
    except KeyError:
        return f"Tool error: unknown tool {name!r}"
    try:
        args = json.loads(raw_args or "{}")
        result = function(**args)
    except Exception as exc:
        return f"Tool error: {exc}"
    return result[:MAX_RESULT_CHARS]


def parse_report(text: str | None) -> dict:
    """The model's final JSON report. Raises ValueError if it isn't one."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    report = json.loads(text)  # json.JSONDecodeError is a ValueError
    if not isinstance(report, dict):
        raise ValueError("report is not a JSON object")
    missing = [key for key in REPORT_KEYS if key not in report]
    if missing:
        raise ValueError(f"report is missing {missing}")
    return {key: report[key] for key in REPORT_KEYS}


def _squash(text) -> str:
    return " ".join(str(text).split())


def _simplify(name: str) -> str:
    name = name.lower().removeprefix("oteldemo.").removesuffix("service")
    return re.sub(r"[^a-z0-9]", "", name)


_SIMPLE_NAMES = {_simplify(s): s for s in KNOWN_SERVICES}


def normalize_service(name) -> tuple[str, bool]:
    """Map a model's service name onto KNOWN_SERVICES. Returns (name, unknown)."""
    name = str(name).strip()
    if name in KNOWN_SERVICES or name == "none":
        return name, False
    if name.lower() == "none":
        return "none", False
    if match := _SIMPLE_NAMES.get(_simplify(name)):
        return match, False
    return name, True


def ungrounded_quotes(evidence, outputs: dict[str, list[str]]) -> list:
    """Evidence quotes that don't appear in the named tool's real outputs."""
    failed = []
    for item in evidence if isinstance(evidence, list) else [evidence]:
        tool = item.get("tool") if isinstance(item, dict) else None
        quote = item.get("quote") if isinstance(item, dict) else item
        needle = _squash(quote) if quote else ""
        seen = [_squash(out) for out in outputs.get(tool, [])]
        if not needle or not any(needle in out for out in seen):
            failed.append(quote)
    return failed


def _call_key(name: str, raw_args: str) -> tuple[str, str]:
    """Same tool + same arguments => same key, whatever the JSON spacing or key order."""
    try:
        return name, json.dumps(json.loads(raw_args or "{}"), sort_keys=True)
    except json.JSONDecodeError:
        return name, raw_args


def investigate(
    alert: str,
    start: str,
    end: str,
    client=None,
    model: str | None = None,
    max_tool_calls: int = 8,
    max_total_tokens: int = 60000,
    log_path: str | None = None,
    on_step: Callable[[dict], None] | None = None,
) -> dict:
    """Investigate one alert. Returns the report plus run statistics."""
    client = client or default_client()
    model = model or os.getenv("SPANHOUND_MODEL", "openai/gpt-oss-120b")
    temperature = float(os.getenv("SPANHOUND_TEMPERATURE", "0"))
    started = time.monotonic()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Alert: {alert}\nWindow (UTC): {start} to {end}"},
    ]
    usage = {"prompt": 0, "completion": 0}
    tool_calls = 0
    stopped_by = "answer"
    outputs: dict[str, list[str]] = {}  # real tool outputs this run, by tool name
    cache: dict[tuple[str, str], str] = {}  # (tool, arguments) -> result

    provider_retries = 0

    def ask(tool_choice: str = "auto"):
        """One model call. A "Harmony parsing failed" 500 is retried at a higher temperature."""
        nonlocal provider_retries
        temp = temperature
        for attempt in range(HARMONY_RETRIES + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice=tool_choice,
                    parallel_tool_calls=False,
                    temperature=temp,
                )
                break
            except openai.InternalServerError as exc:
                if "Harmony parsing failed" not in str(exc) or attempt == HARMONY_RETRIES:
                    raise
                provider_retries += 1
                temp = HARMONY_RETRY_TEMPERATURE
        if response.usage:
            usage["prompt"] += response.usage.prompt_tokens
            usage["completion"] += response.usage.completion_tokens
        return response.choices[0].message

    def log_step(name: str, raw_args: str, result: str) -> None:
        step = {
            "step": tool_calls,
            "tool": name,
            "arguments": raw_args,
            "result_chars": len(result),
            "tokens_so_far": usage["prompt"] + usage["completion"],
        }
        if on_step:
            on_step(step)
        if log_path:
            with open(log_path, "a") as f:
                f.write(json.dumps(step) + "\n")

    def converse() -> tuple[dict | None, str]:
        """The tool loop and final answer. Returns (parsed report or None, last reply text)."""
        nonlocal tool_calls, stopped_by
        reply = ask()
        while reply.tool_calls:
            # Only the first call is kept: any later ones were written before the model
            # saw a real result, so they may rest on results it made up.
            call = reply.tool_calls[0]
            name, raw_args = call.function.name, call.function.arguments
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": name, "arguments": raw_args},
                        }
                    ],
                }
            )

            tool_calls += 1
            key = _call_key(name, raw_args)
            if key in cache:
                result = f"{REPEAT_PREFIX}\n{cache[key]}"
            else:
                result = run_tool(name, raw_args)
                cache[key] = result
                outputs.setdefault(name, []).append(result)
            log_step(name, raw_args, result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

            if tool_calls >= max_tool_calls:
                stopped_by = "tool_budget"
            elif usage["prompt"] + usage["completion"] >= max_total_tokens:
                stopped_by = "token_budget"
            if stopped_by != "answer":
                messages.append({"role": "user", "content": BUDGET_MESSAGE})
                reply = ask(tool_choice="none")
                break
            reply = ask()

        try:
            return parse_report(reply.content), reply.content or ""
        except ValueError:
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": RETRY_MESSAGE})
            reply = ask(tool_choice="none")
        try:
            return parse_report(reply.content), reply.content or ""
        except ValueError:
            return None, reply.content or ""

    unknown, failed, grounded = False, [], False
    try:
        report, last_text = converse()
    except openai.APIError as exc:
        stopped_by = "provider_error"
        first_line = (str(exc).splitlines() or [""])[0]
        report = {
            "suspected_service": "error",
            "cause": f"{type(exc).__name__}: {first_line}"[:300],
            "evidence": [],
            "confidence": 0,
        }
    else:
        if report is None:
            report = {
                "suspected_service": "unparseable",
                "cause": last_text,
                "evidence": [],
                "confidence": 0,
            }
        else:
            report["suspected_service"], unknown = normalize_service(report["suspected_service"])
            failed = ungrounded_quotes(report["evidence"], outputs)
            grounded = not failed

    return {
        **report,
        "grounded": grounded,
        "ungrounded_quotes": failed,
        "unknown_service": unknown,
        "tool_calls": tool_calls,
        "provider_retries": provider_retries,
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
        "seconds": round(time.monotonic() - started, 1),
        "stopped_by": stopped_by,
    }
