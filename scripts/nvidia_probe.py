"""Standalone smoke test for the NVIDIA NIM endpoint -- not part of the bot.

Run it on its own to check the key works and to see, with your own eyes,
whether this model leaks its "thinking" into the plain answer text. That
second question matters because bot/services/llm/openai_compatible.py (what
the real bot uses) only ever reads response.choices[0].message.content -- it
has no idea about a separate reasoning_content field. If reasoning text shows
up mixed into the plain answer below with enable_thinking=True, the same
would happen inside the bot's actual parsing/ranking calls.

Usage:
    export NVIDIA_API_KEY=nvapi-...          # put the real key here, in your shell -- never in this file or in chat
    python scripts/nvidia_probe.py
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

API_KEY_ENV_VAR = "NVIDIA_API_KEY"


def main() -> int:
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        print(
            f"{API_KEY_ENV_VAR} is not set. Export it in your shell first, e.g.:\n"
            f"  export {API_KEY_ENV_VAR}=nvapi-...\n"
            "Never paste the key directly into this file or into a chat message --"
            " anything typed into a shared session should be treated as exposed.",
            file=sys.stderr,
        )
        return 2

    client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)

    completion = client.chat.completions.create(
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        messages=[{"role": "user", "content": "Write a limerick about the wonders of GPU computing."}],
        temperature=1,
        top_p=0.95,
        max_tokens=16384,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}, "reasoning_budget": 16384},
        stream=True,
    )

    print("--- reasoning_content (if any; the bot's real code never reads this) ---")
    saw_reasoning = False
    saw_content = False
    for chunk in completion:
        if not chunk.choices:
            continue
        reasoning = getattr(chunk.choices[0].delta, "reasoning_content", None)
        if reasoning:
            saw_reasoning = True
            print(reasoning, end="", flush=True)
        content = chunk.choices[0].delta.content
        if content is not None:
            if not saw_content:
                print("\n--- content (this is the only field the real bot ever reads) ---")
                saw_content = True
            print(content, end="", flush=True)
    print()

    print("\n--- verdict ---")
    if saw_reasoning and saw_content:
        print("Clean separation: reasoning stayed in reasoning_content, answer stayed in content.")
        print("Good -- the bot's real code (which only reads .content) would have gotten just the answer.")
    elif saw_content and not saw_reasoning:
        print("No reasoning_content came through at all -- either thinking was skipped, or this")
        print("particular request didn't trigger it. Re-run a few times before concluding either way.")
    else:
        print("Unexpected: no clean 'content' stream observed. Something is off -- check the raw output above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
