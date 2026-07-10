from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SPECFLOW_ROOT", ".specflow"))
FLOW = ROOT / "flow.json"
PLAN = ROOT / "plan.json"
DRAFT = ROOT / "draft.json"
SPEC = ROOT / "SPEC.md"
STATE = ROOT / "state.json"


class GateError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise GateError(f"missing {path}")
    return json.loads(path.read_text())


def _read_optional(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _validate(flow: dict[str, Any]) -> None:
    stages = flow.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise GateError("flow requires stages")
    if flow.get("initial") not in stages:
        raise GateError("invalid initial stage")
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            raise GateError(f"invalid stage: {name}")
        target = stage.get("next")
        if target != "completed" and target not in stages:
            raise GateError(f"invalid next stage: {name}")
        if not stage.get("verify"):
            raise GateError(f"missing verifier: {name}")
        if not isinstance(stage.get("tools", []), list):
            raise GateError(f"invalid tools: {name}")


def _safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise GateError(f"unsafe verifier path: {path}")
    return candidate


def _replace_verifier_root(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return value.replace("{verifier_root}", root)
    if isinstance(value, list):
        return [_replace_verifier_root(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _replace_verifier_root(item, root) for key, item in value.items()}
    return value


def load() -> tuple[dict[str, Any], dict[str, Any]]:
    return _read(FLOW), _read(STATE)


def load_state() -> dict[str, Any]:
    return _read(STATE)


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


def start_plan(plan_path: str, request: str) -> dict[str, Any]:
    plan = json.loads(Path(plan_path).read_text())
    if not isinstance(plan.get("tools", []), list):
        raise GateError("invalid planning tools")
    _write(PLAN, plan)
    state = {
        "status": "planning",
        "stage": "plan",
        "revision": 0,
        "request": request.strip(),
        "latest_feedback": "",
        "resume_stage": None,
        "pending": None,
        "history": [{"event": "plan_started", "request": request.strip()}],
    }
    _write(STATE, state)
    return state


def record_human_message(text: str) -> dict[str, Any]:
    state = load_state()
    if state["status"] != "planning":
        return state
    message = text.strip()
    if message:
        state["latest_feedback"] = message
        state["history"].append({"event": "human_feedback", "text": message})
        _write(STATE, state)
    return state


def propose_flow(spec: str, flow: dict[str, Any], verifiers: list[dict[str, str]]) -> dict[str, Any]:
    state = load_state()
    if state["status"] != "planning":
        raise GateError("flow proposals are only accepted during planning")
    _validate(flow)
    normalized: list[dict[str, str]] = []
    for item in verifiers:
        path = str(item.get("path", ""))
        content = str(item.get("content", ""))
        _safe_relative(path)
        if not content:
            raise GateError(f"empty verifier: {path}")
        normalized.append({"path": path, "content": content})
    if not normalized:
        raise GateError("at least one verifier file is required")

    draft = {"spec": spec.strip(), "flow": flow, "verifiers": normalized}
    digest = _digest(draft)
    _write(DRAFT, draft)
    state["revision"] += 1
    state["pending"] = {
        "kind": "approve_draft",
        "digest": digest,
        "revision": state["revision"],
    }
    state["history"].append({
        "event": "model_proposal",
        "digest": digest,
        "revision": state["revision"],
    })
    _write(STATE, state)
    return {
        "accepted": True,
        "digest": digest,
        "revision": state["revision"],
        "message": "Draft stored. Continue discussion or wait for human %approve.",
    }


def approve(approved: bool, note: str = "") -> dict[str, Any]:
    state = load_state()
    if state["status"] == "planning":
        if not approved:
            state["latest_feedback"] = note
            state["pending"] = None
            state["history"].append({"event": "draft_rejected", "note": note})
            _write(STATE, state)
            return state
        return approve_draft(note)

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


def approve_draft(note: str = "") -> dict[str, Any]:
    state = load_state()
    if state["status"] != "planning":
        raise GateError("not in planning")
    draft = _read(DRAFT)
    _validate(draft["flow"])
    digest = _digest(draft)
    pending = state.get("pending") or {}
    if pending.get("digest") != digest:
        raise GateError("draft changed after proposal")

    verifier_root = ROOT / "verifiers" / digest[:12]
    for item in draft["verifiers"]:
        relative = _safe_relative(item["path"])
        destination = verifier_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"])

    flow = _replace_verifier_root(draft["flow"], verifier_root.as_posix())
    _write(FLOW, flow)
    SPEC.parent.mkdir(parents=True, exist_ok=True)
    SPEC.write_text(draft["spec"].rstrip() + "\n")

    resume_stage = state.get("resume_stage")
    stage = resume_stage if resume_stage in flow["stages"] else flow["initial"]
    state["status"] = "running"
    state["stage"] = stage
    state["pending"] = None
    state["approved_digest"] = digest
    state["latest_feedback"] = ""
    state["history"].append({
        "event": "human_approval",
        "digest": digest,
        "note": note,
        "stage": stage,
    })
    _write(STATE, state)
    return state


def begin_replan(feedback: str = "") -> dict[str, Any]:
    state = load_state()
    current_stage = state.get("stage")
    state["status"] = "planning"
    state["resume_stage"] = current_stage if current_stage != "plan" else state.get("resume_stage")
    state["stage"] = "plan"
    state["latest_feedback"] = feedback.strip()
    state["pending"] = None
    state["history"].append({"event": "replan_started", "feedback": feedback.strip()})
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


def _planning_view(state: dict[str, Any]) -> dict[str, Any]:
    plan = _read(PLAN)
    draft = _read_optional(DRAFT)
    return {
        "workflow": plan.get("name", "plan"),
        "status": state["status"],
        "stage": "plan",
        "revision": state["revision"],
        "request": state.get("request", ""),
        "objective": plan.get("objective", "Create an executable spec and flow."),
        "instructions": plan.get("instructions", []),
        "tools": plan.get("tools", []),
        "tool_schemas": plan.get("tool_schemas", {}),
        "latest_feedback": state.get("latest_feedback", ""),
        "draft": None if not draft else {
            "digest": _digest(draft),
            "spec": draft.get("spec", ""),
            "stages": list(draft.get("flow", {}).get("stages", {})),
            "verifiers": [item.get("path") for item in draft.get("verifiers", [])],
        },
        "pending": state.get("pending"),
    }


def view() -> dict[str, Any]:
    state = load_state()
    if state["status"] == "planning":
        return _planning_view(state)

    flow = _read(FLOW)
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
    if current["status"] == "planning":
        lines = [
            "Mode: plan",
            f"Goal: {current['request']}",
            f"Objective: {current['objective']}",
        ]
        if current["latest_feedback"]:
            lines.append(f"Latest human feedback: {current['latest_feedback']}")
        if current["instructions"]:
            lines.append("Instructions:")
            lines.extend(f"- {item}" for item in current["instructions"])
        lines.append(
            "Discuss observable behavior and verification with the human. "
            "When ready, call specflow.propose_flow. Do not implement production changes."
        )
        return "\n".join(lines)

    if current["status"] != "running":
        return f"Specflow is {current['status']}. Do not work; wait for human decision."
    lines = [
        f"Stage: {current['stage']}",
        f"Objective: {current['objective']}",
    ]
    if current["instructions"]:
        lines.append("Instructions:")
        lines.extend(f"- {item}" for item in current["instructions"])
    lines.append("Use only the tools exposed for this stage. Call specflow.request_change if the approved flow must change.")
    return "\n".join(lines)


def _active_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state()
    if state["status"] == "planning":
        return _read(PLAN), state
    if state["status"] == "running":
        flow = _read(FLOW)
        return flow["stages"][state["stage"]], state
    raise GateError(f"workflow is {state['status']}")


def policy(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        contract, state = _active_contract()
    except GateError as exc:
        return {"permissionDecision": "deny", "reason": str(exc)}

    if tool not in contract.get("tools", []):
        return {"permissionDecision": "deny", "reason": f"{tool} unavailable in {state['stage']}"}
    if tool == "shell":
        command = str(args.get("command", ""))
        prefixes = contract.get("shell_prefixes", [])
        if not any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes):
            return {"permissionDecision": "deny", "reason": "command outside stage policy"}
    if tool == "write":
        path = str(args.get("path") or args.get("file_path") or "")
        if path == ROOT.as_posix() or path.startswith(ROOT.as_posix() + "/"):
            return {"permissionDecision": "deny", "reason": "control files are read-only to the model"}
        allowed = contract.get("write_paths")
        if allowed and not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed):
            return {"permissionDecision": "deny", "reason": "path outside stage policy"}
    return {"permissionDecision": "allow", "stage": state["stage"]}


def request_change(reason: str, proposal: str = "") -> dict[str, Any]:
    state = load_state()
    if state["status"] != "running":
        raise GateError("change requests are only accepted while running")
    state["status"] = "human_pending"
    state["pending"] = {
        "kind": "workflow_change",
        "reason": reason,
        "proposal": proposal,
    }
    state["history"].append({"event": "model_change_request", **state["pending"]})
    _write(STATE, state)
    return {"accepted": True, "message": "Work paused for human decision"}


def _run_verifier(command: str | list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)


def stop() -> dict[str, Any]:
    state = load_state()
    if state["status"] == "planning":
        return {
            "decision": "block",
            "reason": "planning requires an approved draft",
            "prompt": prompt(),
        }
    if state["status"] != "running":
        return {"decision": "block", "reason": f"workflow is {state['status']}"}

    flow = _read(FLOW)
    stage_name = state["stage"]
    stage = flow["stages"][stage_name]
    process = _run_verifier(stage["verify"], stage.get("timeout", 120))
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
