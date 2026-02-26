#!/usr/bin/env python3
"""MCP approval server — bridges Claude CLI permission requests to the mobile app.

Spawned by Claude CLI via --mcp-config when --permission-prompt-tool is set.
Communicates with conversation_server.py via HTTP long-poll to relay permission
requests to the iOS app and return user decisions.

Flow:
  1. Claude CLI calls this tool when it needs permission to use a tool
  2. This server POSTs to conversation_server /internal/permission-request
  3. Conversation server pushes the request to iOS via WebSocket
  4. User taps Allow/Deny on their phone
  5. Conversation server returns the response to this server's HTTP request
  6. This server returns allow/deny to Claude CLI
"""

import asyncio
import json
import os
import sys
import urllib.request
import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("approval")

CONV_SERVER = os.environ.get("APPROVAL_SERVER_URL", "http://127.0.0.1:8081")


def _summarize_tool(name: str, input_data: dict) -> str:
    """One-line human-readable summary of what Claude wants to do."""
    if name == "Bash":
        cmd = input_data.get("command", "")
        desc = input_data.get("description", "")
        if desc:
            return desc
        return f"$ {cmd[:120]}" if cmd else "Run command"
    elif name == "Write":
        path = input_data.get("file_path", "?")
        return f"Write to {os.path.basename(path)}"
    elif name == "Edit":
        path = input_data.get("file_path", "?")
        return f"Edit {os.path.basename(path)}"
    elif name == "Read":
        path = input_data.get("file_path", "?")
        return f"Read {os.path.basename(path)}"
    elif name == "Glob":
        return f"Search: {input_data.get('pattern', '?')}"
    elif name == "Grep":
        return f"Grep: {input_data.get('pattern', '?')}"
    elif name == "WebFetch":
        return f"Fetch {input_data.get('url', '?')[:80]}"
    elif name == "WebSearch":
        return f"Search web: {input_data.get('query', '?')}"
    elif name == "Task":
        return f"Agent: {input_data.get('description', '?')}"
    return f"Use tool: {name}"


def _http_request(request_id, tool_name, input_data, summary):
    """Blocking HTTP long-poll to conversation server. Runs in thread pool."""
    payload = json.dumps({
        "request_id": request_id,
        "tool_name": tool_name,
        "input": input_data,
        "summary": summary,
    }).encode()

    req = urllib.request.Request(
        f"{CONV_SERVER}/internal/permission-request",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Long timeout — user may take a while to respond on mobile
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


@mcp.tool()
async def approve(tool_use_id: str, tool_name: str, input: dict) -> str:
    """Process a permission request from Claude Code.

    Called automatically when Claude wants to use a tool that requires approval.
    Forwards the request to the mobile app and waits for the user's decision.

    Args:
        tool_use_id: Unique identifier for this tool invocation
        tool_name: Name of the tool Claude wants to use (e.g. "Bash", "Write")
        input: The parameters Claude wants to pass to the tool

    Returns:
        JSON string with behavior:"allow" or behavior:"deny"
    """
    request_id = str(uuid.uuid4())[:8]
    summary = _summarize_tool(tool_name, input)

    try:
        result = await asyncio.to_thread(
            _http_request, request_id, tool_name, input, summary
        )

        if result.get("allow"):
            response = {
                "behavior": "allow",
                "updatedInput": result.get("updatedInput", input),
            }
        else:
            response = {
                "behavior": "deny",
                "message": result.get("message", "User denied permission"),
            }
    except Exception as e:
        # On timeout or error, deny by default (safe)
        response = {
            "behavior": "deny",
            "message": f"Permission bridge error: {e}",
        }

    return json.dumps(response)


if __name__ == "__main__":
    mcp.run(transport="stdio")
