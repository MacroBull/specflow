from __future__ import annotations

import json
import sys

from .engine import load_state, propose_flow, request_change, view

CURRENT_STAGE = {
    "name": "specflow.current_stage",
    "description": "Return the authoritative current mode, objective, feedback, capabilities and typed tool schemas.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}

PROPOSE_FLOW = {
    "name": "specflow.propose_flow",
    "description": "Submit a structured spec, staged flow and verifier files for human review. This does not approve the flow.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "spec": {"type": "string", "minLength": 1},
            "flow": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "initial": {"type": "string"},
                    "stages": {"type": "object", "minProperties": 1},
                },
                "required": ["name", "initial", "stages"],
                "additionalProperties": False,
            },
            "verifiers": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "content": {"type": "string", "minLength": 1},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["spec", "flow", "verifiers"],
        "additionalProperties": False,
    },
}

REQUEST_CHANGE = {
    "name": "specflow.request_change",
    "description": "Pause execution and ask a human to revise the approved workflow.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            "proposal": {"type": "string"},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}


def available_tools() -> list[dict]:
    state = load_state()
    if state["status"] == "planning":
        return [CURRENT_STAGE, PROPOSE_FLOW]
    if state["status"] == "running":
        return [CURRENT_STAGE, REQUEST_CHANGE]
    return [CURRENT_STAGE]


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
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "specflow", "version": "0.2.0"},
                })
            elif method == "tools/list":
                send(request_id, {"tools": available_tools()})
            elif method == "tools/call":
                name = request["params"]["name"]
                args = request["params"].get("arguments", {})
                if name == "specflow.current_stage":
                    output = view()
                elif name == "specflow.propose_flow":
                    output = propose_flow(args["spec"], args["flow"], args["verifiers"])
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
