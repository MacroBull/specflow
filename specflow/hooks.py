from __future__ import annotations

import json
import os
import sys

from .engine import (
    GateError,
    approve,
    begin_replan,
    bootstrap,
    load_state,
    policy,
    prompt,
    record_human_message,
    start_plan,
    stop,
    view,
)


def _input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _reply(context: str = "", **extra) -> None:
    output = {"continue": True, **extra}
    if context:
        output["additionalContext"] = context
    print(json.dumps(output))


def _submit(text: str) -> None:
    stripped = text.strip()
    if stripped.startswith("%"):
        head, _, tail = stripped[1:].partition(" ")
        name = head.strip()
        argument = tail.strip()
        if name == "plan":
            plan_path = os.environ.get("SPECFLOW_PLAN", ".specflow/tools/plan.json")
            start_plan(plan_path, argument)
            _reply(prompt())
            return
        if name == "approve":
            approve(True, argument)
            _reply(prompt())
            return
        if name in {"reject", "revise"}:
            state = load_state()
            if state["status"] == "planning":
                approve(False, argument)
            else:
                begin_replan(argument)
            _reply(prompt())
            return
        if name == "status":
            _reply(json.dumps(view()))
            return

        flow = os.environ.get("SPECFLOW_FLOW", f".specflow/tools/{name}.json")
        if not os.path.exists(".specflow/state.json"):
            bootstrap(flow)
        _reply(prompt())
        return

    if os.path.exists(".specflow/state.json"):
        record_human_message(text)
        _reply(prompt())
    else:
        _reply()


def main() -> None:
    event = os.environ.get("SPECFLOW_EVENT", "")
    data = _input()
    try:
        if event == "UserPromptSubmit":
            _submit(data.get("prompt", ""))
        elif event == "PreToolUse":
            print(json.dumps(policy(data.get("tool_name", ""), data.get("tool_input", {}))))
        elif event == "Stop":
            print(json.dumps(stop()))
        else:
            print("{}")
    except GateError as exc:
        print(json.dumps({"decision": "block", "reason": str(exc)}))
        raise SystemExit(2)
