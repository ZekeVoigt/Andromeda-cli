#!/usr/bin/env python3
"""A minimal MCP server over stdio, for testing the client against the wire.

Real newline-delimited JSON-RPC: initialize, tools/list, tools/call. Small
enough to read in a minute, which is the point — a mocked transport would test
the mock.
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Return whatever it is given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "explode",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def reply(identifier, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    # Servers that log to stdout are common; the client must ignore lines that
    # are not JSON-RPC, so this one emits some.
    sys.stdout.write("starting up\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        identifier = message.get("id")

        if method == "initialize":
            reply(
                identifier,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo", "version": "0.0.1"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                reply(identifier, {"content": [{"type": "text", "text": arguments.get("text", "")}]})
            elif name == "add":
                total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
                reply(identifier, {"content": [{"type": "text", "text": str(total)}]})
            elif name == "explode":
                reply(
                    identifier,
                    {"content": [{"type": "text", "text": "it broke"}], "isError": True},
                )
            else:
                reply(identifier, error={"code": -32601, "message": f"no tool {name}"})
        elif identifier is not None:
            reply(identifier, error={"code": -32601, "message": f"no method {method}"})


if __name__ == "__main__":
    main()
