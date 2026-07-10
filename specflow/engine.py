from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SPECFLOW_ROOT", ".specflow"))
FLOW = ROOT / "flow.json"
STATE = ROOT / "state.json"


class GateError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise GateError(f"missing {path}")
    return json.loads(path.read_text())


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def _validate(flow: dict[str, Any]) -> None:
    if flow.get("initial") not in flow.get("stages", {}):
        raise GateError("invalid initial stage")
    for name, stage in flow["stages"].items():
        target = stage.get("next")
        if target != "completed" and target not in flow["stages"]:
            raise GateError(f"invalid next stage: {name}")


def load() -> tuple[dict[str, Any], dict[str, Any]]:
    return _read(FLOW), _read(STATE)


def bootstrap(flow_path: str) -> dict[str, Any]:
    flow = json.loads(Path(flow_path).read_text())
    _validate(flow)
    _write(FLOW, flow)
    state = {
        "status": "approval_pending",
        "stage": flow["initial"],
        "revision": 1,
        "pending": {"kind": "approve_spec"},
        "history": [],
    }
    _write(STATE, state)
    return state


def approve(approved: bool, note: str = "") -> dict[str, Any]:
    _, state = load()
    if not state.get("pending"):
        raise GateError("no decision pending")
    state["history"].append({
        "event": "human_decision",
        "approved": approved,
        "note": note,
        "pending": state["pending"],
    })
    state["status"] = "running" if approved else "change_requested"
    state["pending"] = None
    _write(STATE, state)
    return state


def amend(flow_path: str) -> dict[str, Any]:
    _, state = load()
    flow = json.loads(Path(flow_path).read_text())
    _validate(flow)
    if state["stage"] not in flow["stages"]:
        raise GateError("current stage missing from amended flow")
    _write(FLOW, flow)
    state["revision"] += 1
    state["status"] = "approval_pending"
    state["pending"] = {"kind": "approve_spec"}
    _write(STATE, state)
    return state


def view() -> dict[str, Any]:
    flow, state = load()
    stage = flow["stages"].get(state["stage"], {}) if state["stage"] != "completed" else {}
    return {
        "workflow": flow.get("name"),
        "status": state["status"],
        "stage": state["stage"],
        "revision": state["revision"],
        "objective": stage.get("objective", ""),
        "instructions": stage.get("instructions", []),
        "tools": stage.get("tools", []),
        "tool_schemas": stage.get("tool_schemas", {}),
        "pending": state.get("pending"),
    }


def prompt() -> str:
    current = view()
    if current["status"] != "running":
        return f"Specflow is {current['status']}. Do not work; wait for human decision."
    lines = [
        f"Stage: {current['stage']}",
        f"Objective: {current['objective']}",
    ]
    if current["instructions"]:
        lines.append("Instructions:")
        lines.extend(f"- {item}" for item in current["instructions"])
    lines.append("Use only the tools exposed for this stage. Call specflow.request_change when the approved flow is insufficient.")
    return "\n".join(lines)


def policy(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    flow, state = load()
    if state["status"] != "running":
        return {"permissionDecision": "deny", "reason": f"workflow is {state['status']}"}
    stage = flow["stages"][state["stage"]]
    if tool not in stage.get("tools", []):
        return {"permissionDecision": "deny", "reason": f"{tool} unavailable in {state['stage']}"}
    if tool == "shell":
        command = str(args.get("command", ""))
        prefixes = stage.get("shell_prefixes", [])
        if not any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes):
            return {"permissionDecision": "deny", "reason": "command outside stage policy"}
    return {"permissionDecision": "allow", "stage": state["stage"]}


def request_change(reason: str, proposal: str = "") -> dict[str, Any]:
    _, state = load()
    state["status"] = "human_pending"
    state["pending"] = {
        "kind": "workflow_change",
        "reason": reason,
        "proposal": proposal,
    }
    _write(STATE, state)
    return {"accepted": True, "message": "Work paused for human decision"}


def stop() -> dict[str, Any]:
    flow, state = load()
    if state["status"] != "running":
        return {"decision": "block", "reason": f"workflow is {state['status']}"}

    stage_name = state["stage"]
    stage = flow["stages"][stage_name]
    process = subprocess.run(
        stage["verify"],
        shell=True,
        text=True,
        capture_output=True,
        timeout=stage.get("timeout", 120),
    )
    evidence = {
        "exit_code": process.returncode,
        "stdout": process.stdout[-2000:],
        "stderr": process.stderr[-2000:],
    }
    state["history"].append({"event": "verify", "stage": stage_name, **evidence})

    if process.returncode:
        _write(STATE, state)
        return {
            "decision": "block",
            "reason": "stage gate failed",
            "stage": stage_name,
            "evidence": evidence,
            "prompt": prompt(),
        }

    target = stage["next"]
    state["history"].append({"event": "transition", "from": stage_name, "to": target})
    state["stage"] = target
    if target == "completed":
        state["status"] = "completed"
        _write(STATE, state)
        return {"decision": "allow", "completed": True}

    _write(STATE, state)
    return {
        "decision": "block",
        "reason": "continue in next stage",
        "stage": target,
        "prompt": prompt(),
    }
