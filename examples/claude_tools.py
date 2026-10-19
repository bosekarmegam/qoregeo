"""
Give Claude qoregeo's GIS tools with the Claude API (no MCP needed).

    pip install qoregeo anthropic
    export ANTHROPIC_API_KEY=...
    python examples/claude_tools.py stores.csv "Which 3 stores are closest to Connaught Place, Delhi?"

The tools run locally, confined to the folder of the data file; Claude only sees
their JSON results — and the PNG from preview_map, so it can look at the map.
"""

from __future__ import annotations

import os
import sys

import anthropic

from qoregeo.tools import ToolContext, call_tool, tool_definitions

MODEL = "claude-opus-5-5"


def main(data_file: str, question: str) -> None:
    client = anthropic.Anthropic()
    context = ToolContext(root=os.path.dirname(os.path.abspath(data_file)), allow_write=False)
    tools = tool_definitions("anthropic", include_network=False)
    messages: list[dict] = [{
        "role": "user",
        "content": f"The dataset is '{os.path.basename(data_file)}'. {question}",
    }]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system="You are a GIS analyst. Use the qoregeo tools; check results with preview_map.",
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        # Run every tool call of this turn and send all results back in one message.
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"→ {block.name} {block.input}", file=sys.stderr)
                results.append(call_tool(block.name, block.input, context).to_anthropic(block.id))
        messages.append({"role": "user", "content": results})

    print("".join(b.text for b in response.content if b.type == "text"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
