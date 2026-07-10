from __future__ import annotations

import json
import os
import sys

from .engine import GateError, bootstrap, policy, prompt, stop


def _input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def main() -> None:
    event = os.environ.get("SPECFLOW_EVENT", "")
    data = _input()
    try:
        if event == "UserPromptSubmit":
            text = data.get("prompt", "")
            if text.startswith("%"):
                tool_name = text[1:].split(maxsplit=1)[0]
                flow = os.environ.get("SPECFLOW_FLOW", f".specflow/tools/{tool_name}.json")
                if not os.path.exists(".specflow/state.json"):
                    bootstrap(flow)
                print(json.dumps({"continue": True, "additionalContext": prompt()}))
            else:
                print(json.dumps({"continue": True}))
        elif event == "PreToolUse":
            print(json.dumps(policy(data.get("tool_name", ""), data.get("tool_input", {}))))
        elif event == "Stop":
            print(json.dumps(stop()))
        else:
            print("{}")
    except GateError as exc:
        print(json.dumps({"decision": "block", "reason": str(exc)}))
        raise SystemExit(2)
