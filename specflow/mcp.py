from __future__ import annotations

import json
import sys

from .engine import request_change, view

TOOLS = [
    {
        "name": "specflow.current_stage",
        "description": "Return the authoritative current stage, objective, instructions and capabilities.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "specflow.request_change",
        "description": "Pause work and ask a human to approve a workflow change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "proposal": {"type": "string"},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


def send(request_id, result) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


def main() -> None:
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "initialize":
                send(request_id, {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "specflow", "version": "0.1.0"},
                })
            elif method == "tools/list":
                send(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                name = request["params"]["name"]
                args = request["params"].get("arguments", {})
                if name == "specflow.current_stage":
                    output = view()
                elif name == "specflow.request_change":
                    output = request_change(args["reason"], args.get("proposal", ""))
                else:
                    raise ValueError(f"unknown tool: {name}")
                send(request_id, {
                    "content": [{"type": "text", "text": json.dumps(output)}],
                    "structuredContent": output,
                })
            elif request_id is not None:
                send(request_id, {})
        except Exception as exc:
            if request_id is not None:
                send(request_id, {
                    "isError": True,
                    "content": [{"type": "text", "text": str(exc)}],
                })
