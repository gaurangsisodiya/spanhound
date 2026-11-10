"""Agent loop tests with a scripted fake model: no network, no database."""

import json
from types import SimpleNamespace

import httpx2
import openai
import pytest

from sentinel import agent

REPORT = {
    "suspected_service": "payment",
    "cause": "payment rejected charges",
    "evidence": [{"tool": "error_origins", "quote": "payment / charge"}],
    "confidence": 0.9,
}
WINDOW = ("2026-11-10 15:52:00", "2026-11-10 15:54:00")


def tool_call(name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


def tool_reply(name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(content=None, tool_calls=[tool_call(name, args, call_id)])


def counting_tool(monkeypatch, name: str, output: str) -> list:
    """Replace one tool with a fake that records each call; returns the record list."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return output

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, name, fake)
    return calls


def text_reply(text: str):
    return SimpleNamespace(content=text, tool_calls=None)


class FakeClient:
    """Stands in for openai.OpenAI. `respond(kwargs)` returns the next message."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        # Copy the message list: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        message = self.respond(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
        )


def scripted(*messages):
    """A respond function that returns these messages in order."""
    queue = list(messages)
    return lambda kwargs: queue.pop(0)


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
    """Replace every real tool with one that returns fixed text."""
    for name in agent.TOOL_FUNCTIONS:
        monkeypatch.setitem(agent.TOOL_FUNCTIONS, name, lambda **kw: "- payment / charge (Server) x3: boom")


def test_one_tool_call_then_answer():
    client = FakeClient(scripted(
        tool_reply("error_origins", {"start": WINDOW[0], "end": WINDOW[1]}),
        text_reply("```json\n" + json.dumps(REPORT) + "\n```"),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert {k: result[k] for k in agent.REPORT_KEYS} == REPORT
    assert result["tool_calls"] == 1
    assert result["stopped_by"] == "answer"
    tool_messages = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_messages == [
        {"role": "tool", "tool_call_id": "call_1", "content": "- payment / charge (Server) x3: boom"}
    ]


def test_stops_at_tool_budget():
    def respond(kwargs):
        if kwargs["tool_choice"] == "none":
            return text_reply(json.dumps(REPORT))
        return tool_reply("slow_operations", {"start": WINDOW[0], "end": WINDOW[1]})

    client = FakeClient(respond)
    result = agent.investigate("checkout slow", *WINDOW, client=client, model="fake", max_tool_calls=3)

    assert result["tool_calls"] == 3
    assert result["stopped_by"] == "tool_budget"
    assert result["suspected_service"] == "payment"
    last = client.requests[-1]
    assert last["tool_choice"] == "none"
    assert last["messages"][-1] == {"role": "user", "content": agent.BUDGET_MESSAGE}


def test_tool_error_is_sent_to_model_and_run_continues(monkeypatch):
    def broken(**kwargs):
        raise ValueError("window is 2:00:00; the maximum is 1:00:00")

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "error_origins", broken)
    client = FakeClient(scripted(
        tool_reply("error_origins", {"start": WINDOW[0], "end": "2026-11-10 17:52:00"}),
        text_reply(json.dumps(REPORT)),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert result["stopped_by"] == "answer"
    assert result["suspected_service"] == "payment"
    tool_message = next(m for m in client.requests[-1]["messages"] if m["role"] == "tool")
    assert tool_message["content"] == "Tool error: window is 2:00:00; the maximum is 1:00:00"


def test_non_json_answer_retried_once_then_unparseable():
    client = FakeClient(scripted(
        text_reply("I think it was payment."),
        text_reply("Definitely payment."),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert result["suspected_service"] == "unparseable"
    assert result["cause"] == "Definitely payment."
    assert len(client.requests) == 2
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": agent.RETRY_MESSAGE}


def test_only_first_of_several_tool_calls_runs(monkeypatch):
    origins = counting_tool(monkeypatch, "error_origins", "- payment / charge (Server) x3: boom")
    logs = counting_tool(monkeypatch, "error_logs", "- payment x1: fake")
    args = {"start": WINDOW[0], "end": WINDOW[1]}
    client = FakeClient(scripted(
        SimpleNamespace(content=None, tool_calls=[
            tool_call("error_origins", args, "call_1"),
            tool_call("error_logs", args, "call_2"),
            tool_call("get_trace", {"trace_id": "a" * 32}, "call_3"),
        ]),
        text_reply(json.dumps(REPORT)),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert len(origins) == 1 and logs == []
    assert result["tool_calls"] == 1
    sent = client.requests[1]["messages"]
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_1"]
    assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == ["call_1"]
    assert client.requests[0]["parallel_tool_calls"] is False


def test_made_up_quote_is_ungrounded():
    made_up = "inventory-service: database connection timeout"
    report = {**REPORT, "evidence": [
        {"tool": "error_origins", "quote": "payment  /   charge"},  # real, odd spacing
        {"tool": "error_origins", "quote": made_up},
    ]}
    client = FakeClient(scripted(
        tool_reply("error_origins", {"start": WINDOW[0], "end": WINDOW[1]}),
        text_reply(json.dumps(report)),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert result["grounded"] is False
    assert result["ungrounded_quotes"] == [made_up]


def test_quote_from_wrong_tool_is_ungrounded():
    report = {**REPORT, "evidence": [{"tool": "error_logs", "quote": "payment / charge"}]}
    client = FakeClient(scripted(
        tool_reply("error_origins", {"start": WINDOW[0], "end": WINDOW[1]}),
        text_reply(json.dumps(report)),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert result["grounded"] is False


@pytest.mark.parametrize(
    "raw, expected, unknown",
    [
        ("payment", "payment", False),
        ("oteldemo.PaymentService", "payment", False),
        ("ProductCatalogService", "product-catalog", False),
        ("frontend-proxy", "frontend-proxy", False),
        ("None", "none", False),
        ("inventory-service", "inventory-service", True),
    ],
)
def test_normalize_service(raw, expected, unknown):
    assert agent.normalize_service(raw) == (expected, unknown)


def test_unknown_service_is_flagged_in_result():
    client = FakeClient(scripted(text_reply(json.dumps({**REPORT, "suspected_service": "inventory-service"}))))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert result["suspected_service"] == "inventory-service"
    assert result["unknown_service"] is True


def test_repeat_call_uses_cached_result(monkeypatch):
    calls = counting_tool(monkeypatch, "error_origins", "- payment / charge (Server) x3: boom")
    client = FakeClient(scripted(
        tool_reply("error_origins", {"start": WINDOW[0], "end": WINDOW[1]}, "call_1"),
        tool_reply("error_origins", {"end": WINDOW[1], "start": WINDOW[0]}, "call_2"),  # same args, other order
        text_reply(json.dumps(REPORT)),
    ))

    result = agent.investigate("checkout fails", *WINDOW, client=client, model="fake")

    assert len(calls) == 1
    assert result["tool_calls"] == 2
    tool_messages = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_messages[1]["content"] == (
        f"{agent.REPEAT_PREFIX}\n- payment / charge (Server) x3: boom"
    )


def harmony_error():
    request = httpx2.Request("POST", "https://api.together.xyz/v1/chat/completions")
    return openai.InternalServerError(
        "Error code: 500 - Harmony parsing failed: Failed to process token 200012",
        response=httpx2.Response(500, request=request),
        body=None,
    )


def test_harmony_error_is_retried_at_higher_temperature():
    attempts = []

    def respond(kwargs):
        attempts.append(kwargs["temperature"])
        if len(attempts) == 1:
            raise harmony_error()
        return text_reply(json.dumps({**REPORT, "evidence": []}))

    result = agent.investigate("checkout fails", *WINDOW, client=FakeClient(respond), model="fake")

    assert result["provider_retries"] == 1
    assert result["stopped_by"] == "answer"
    assert result["suspected_service"] == "payment"
    assert attempts == [0.0, agent.HARMONY_RETRY_TEMPERATURE]


def test_repeated_harmony_errors_become_provider_error():
    def respond(kwargs):
        raise harmony_error()

    result = agent.investigate("checkout fails", *WINDOW, client=FakeClient(respond), model="fake")

    assert result["stopped_by"] == "provider_error"
    assert result["suspected_service"] == "error"
    assert "Harmony parsing failed" in result["cause"]
    assert result["evidence"] == [] and result["confidence"] == 0
    assert result["grounded"] is False
    assert result["provider_retries"] == agent.HARMONY_RETRIES


def test_other_api_error_becomes_provider_error():
    def respond(kwargs):
        raise openai.APITimeoutError(request=httpx2.Request("POST", "https://api.together.xyz"))

    result = agent.investigate("checkout fails", *WINDOW, client=FakeClient(respond), model="fake")

    assert result["stopped_by"] == "provider_error"
    assert result["provider_retries"] == 0
