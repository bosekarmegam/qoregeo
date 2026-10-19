"""
Connect Claude to the qoregeo MCP server with the Anthropic SDK's tool runner.

    pip install qoregeo "anthropic[mcp]"
    export ANTHROPIC_API_KEY=...
    python examples/claude_mcp.py ./data "Make a heatmap of sales.csv weighted by revenue"

The same server works in Claude Code, Claude Desktop, Codex, Cursor, VS Code and
Gemini CLI — see the README's "AI assistants" section.
"""

from __future__ import annotations

import asyncio
import sys

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-opus-5-5"


async def main(root: str, task: str) -> None:
    client = AsyncAnthropic()
    server = StdioServerParameters(command=sys.executable, args=["-m", "qoregeo", "mcp", "--root", root])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as mcp_client:
        await mcp_client.initialize()
        tools = (await mcp_client.list_tools()).tools
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": task}],
            tools=[async_mcp_tool(t, mcp_client) for t in tools],
        )
        async for message in runner:
            for block in message.content:
                if block.type == "text":
                    print(block.text)
                elif block.type == "tool_use":
                    print(f"→ {block.name} {block.input}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
