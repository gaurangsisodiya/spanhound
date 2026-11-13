"""Smoke test: can the model call error_origins and answer from its result?

Run: uv run python scripts/smoke_tool_call.py
Needs TOGETHER_API_KEY in .env and ClickHouse with the incident data.
"""

import json
import os
import sys

import openai
from dotenv import load_dotenv

from spanhound.tools import error_origins

MAX_TOOL_ROUNDS = 3

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "error_origins",
            "description": "Find where errors started in a UTC time window: the deepest failing spans.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "Window start, format YYYY-MM-DD HH:MM:SS, UTC",
                    },
                    "end": {
                        "type": "string",
                        "description": "Window end, format YYYY-MM-DD HH:MM:SS, UTC",
                    },
                },
                "required": ["start", "end"],
            },
        },
    }
]


def run_tool(name: str, raw_args: str) -> str:
    """Run one tool call. Errors go back to the model as text so it can see what went wrong."""
    if name != "error_origins":
        return f"Error: unknown tool {name!r}"
    try:
        args = json.loads(raw_args)
        return error_origins(start=args["start"], end=args["end"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def main() -> int:
    load_dotenv()
    client = openai.OpenAI(
        base_url="https://api.together.xyz/v1",
        api_key=os.environ["TOGETHER_API_KEY"],
        timeout=60,
        max_retries=2,
    )
    model = os.getenv("SPANHOUND_MODEL", "openai/gpt-oss-120b")

    messages = [
        {
            "role": "user",
            "content": (
                "Checkout errors were reported between 2026-11-10 15:52:00 and "
                "2026-11-10 15:54:00 UTC. Use the tool to find where they started, "
                "then answer in one sentence."
            ),
        }
    ]
    prompt_tokens = completion_tokens = 0
    tool_was_called = False

    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
        if response.usage:
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
        message = response.choices[0].message

        if not message.tool_calls:
            break

        tool_was_called = True
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [call.model_dump() for call in message.tool_calls],
            }
        )
        for call in message.tool_calls:
            print(f"Tool call: {call.function.name} {call.function.arguments}")
            result = run_tool(call.function.name, call.function.arguments)
            print(f"Tool result:\n{result}\n")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    else:
        print(f"Model was still calling tools after {MAX_TOOL_ROUNDS} rounds; stopping.")
        print(f"Tokens: {prompt_tokens} prompt, {completion_tokens} completion")
        return 1

    print(f"Answer: {message.content}")
    print(f"Tokens: {prompt_tokens} prompt, {completion_tokens} completion")
    if not tool_was_called:
        print("FAIL: the model answered without calling the tool.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
