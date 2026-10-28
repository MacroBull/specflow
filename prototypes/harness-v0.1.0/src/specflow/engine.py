"""Durable sequential graph executor with test-backed node commits.

This is deliberately not a distributed scheduler. The DAG is topo-sorted once;
independent nodes can be parallelized in a later version without changing the IR.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import subprocess
import uuid
from pathlib import Path
from .agents import execute
from .specs import load_json, save_json, digest
from .graph import graph_hash


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def workspace_path(workspace: Path, rel: str) -> Path:
    p = (workspace / rel).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise ValueError(f"Check path escapes workspace: {rel}")
    return p


def evaluate(check: dict, workspace: Path, timeout: int = 120) -> tuple[bool, str]:
    kind = check["type"]
    if kind in ("file", "contains"):
        path = workspace_path(workspace, check["path"])
        if not path.is_file():
            return False, f"Missing file: {check['path']}"
        if kind == "contains" and check["text"] not in path.read_text(errors="replace"):
            return False, f"Missing literal {check['text']!r} in {check['path']}"
        return True, f"{kind}: {check['path']}"
    if kind == "command":
        argv = check["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("command check needs argv: [program, arg, ...]")
        proc = subprocess.run(argv, cwd=workspace, text=True, capture_output=True, timeout=timeout)
        output = (proc.stdout + "\n" + proc.stderr)[-3000:].strip()
        return proc.returncode == 0, f"command exit {proc.returncode}: {argv!r}\n{output}"
    raise ValueError(f"Unknown check kind: {kind}")


def evaluate_all(checks: list[dict], workspace: Path) -> list[dict]:
    report = []
    for check in checks:
        try:
            ok, detail = evaluate(check, workspace)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, str(exc)
        report.append({"check": check, "ok": ok, "detail": detail})
    return report


def emit(run_dir: Path, event: str, **kwargs) -> None:
    with (run_dir / "events.jsonl").open("a") as file:
        file.write(json.dumps({"time": utc(), "event": event, **kwargs}, ensure_ascii=False) + "\n")


def init_run(root: Path, change: str, graph: dict) -> tuple[Path, dict]:
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = root / ".specflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = {"run_id": run_id, "change": change, "graph_hash": graph_hash(graph), "status": "RUNNING",
             "created_at": utc(), "nodes": {n["id"]: {"status": "PENDING", "attempts": 0} for n in graph["nodes"]}}
    save_json(run_dir / "state.json", state)
    emit(run_dir, "run_created", change=change)
    return run_dir, state


def execute_node(node: dict, root: Path, provider: str, run_dir: Path, feedback: str,
                 *, auto_approve: bool, model: str | None = None) -> str:
    if node["type"] == "verify":
        return "verify-only node"
    if node["type"] == "atomic":
        from .atomic import dispatch
        return dispatch(root, node["op"], node["args"])
    if node["type"] == "command":
        result = subprocess.run(node["argv"], cwd=root, text=True, capture_output=True,
                                timeout=node.get("timeout", 900))
        if result.returncode:
            raise RuntimeError(f"exit={result.returncode}: {(result.stdout + result.stderr)[-2000:]}")
        return result.stdout[-2000:]
    if node["type"] in ("agent", "model"):
        change = load_json(run_dir / "state.json")["change"]
        spec = (root / ".specflow" / "specs" / f"{change}.md").read_text()
        prompt = ("You are implementing a locked SpecFlow DAG node. Finish only this node.\n"
                  f"Locked spec (authoritative task contract):\n{spec[:16000]}\n\n"
                  f"Change: {node.get('change', 'see locked spec')}\nNode: {node['id']}\n"
                  f"Instructions: {node['prompt']}\n"
                  f"Acceptance evidence: {json.dumps(node.get('checks', []), ensure_ascii=False)}\n"
                  f"Prior failure feedback: {feedback or '(none)'}\n"
                  "Your final text is not proof of success; the harness evaluates the checks independently.")
        response = execute(provider, prompt, root,
                           profile=node.get("profile", "reviewer" if node["type"] == "model" else "builder"),
                           timeout=node.get("timeout", 900), auto_approve=auto_approve, model=model,
                           env={"SPECFLOW_RUN_DIR": str(run_dir), "SPECFLOW_NODE_ID": node["id"],
                                "SPECFLOW_PROJECT": str(root)})
        if node["type"] == "model":
            if node.get("format", "text") == "json":
                # A model answer is still untrusted: parsing it does not mean its content is true.
                response = json.dumps(json.loads(response), indent=2, ensure_ascii=False)
            target = workspace_path(root, node["output"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(response + "\n")
        return response
    raise ValueError(f"Unexpected node type: {node['type']}")


def run(root: Path, change: str, *, provider: str, run_id: str | None = None,
        auto_approve: bool = False, model: str | None = None) -> dict:
    sf = root / ".specflow"
    graph = load_json(sf / "locked" / f"{change}.json")
    spec = (sf / "specs" / f"{change}.md").read_text()
    if digest(spec) != graph["spec_hash"]:
        raise ValueError("Locked spec changed. Re-lock explicitly; a locked run is immutable")
    if run_id:
        run_dir = sf / "runs" / run_id
        state = load_json(run_dir / "state.json")
        if state["change"] != change or state["graph_hash"] != graph_hash(graph):
            raise ValueError("Resume request does not match locked graph")
        if state["status"] == "SUCCEEDED":
            return state
        if any(n["status"] == "RUNNING" for n in state["nodes"].values()):
            raise RuntimeError("Interrupted node detected. Use `sf retry RUN_ID NODE_ID` to acknowledge rerun")
    else:
        run_dir, state = init_run(root, change, graph)
    state["status"] = "RUNNING"
    save_json(run_dir / "state.json", state)
    for node in graph["nodes"]:
        item = state["nodes"][node["id"]]
        if item["status"] == "SUCCEEDED":
            continue
        if item["status"] == "FAILED":
            state["status"] = "BLOCKED"
            break
        if any(state["nodes"][d]["status"] != "SUCCEEDED" for d in node["needs"]):
            state["status"] = "BLOCKED"
            break
        feedback = ""
        for attempt in range(node.get("retries", 0) + 1):
            item["attempts"] += 1
            item["status"] = "RUNNING"
            save_json(run_dir / "state.json", state)
            emit(run_dir, "node_started", node=node["id"], attempt=item["attempts"])
            try:
                output = execute_node(node, root, provider, run_dir, feedback, auto_approve=auto_approve, model=model)
                checks = evaluate_all(node.get("checks", []), root)
                good = all(c["ok"] for c in checks)
                feedback = "\n".join(c["detail"] for c in checks if not c["ok"])
                if not good:
                    raise RuntimeError("Node verification failed: " + feedback)
                item["status"] = "SUCCEEDED"
                item["output"] = output[-1200:]
                item["evidence"] = checks
                item["completed_at"] = utc()
                emit(run_dir, "node_succeeded", node=node["id"])
                break
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                feedback = str(exc)
                item["error"] = feedback
                item["status"] = "FAILED"
                emit(run_dir, "node_failed", node=node["id"], error=feedback)
                save_json(run_dir / "state.json", state)
        if item["status"] != "SUCCEEDED":
            state["status"] = "BLOCKED"
            break
        save_json(run_dir / "state.json", state)
    if all(s["status"] == "SUCCEEDED" for s in state["nodes"].values()):
        # Re-verify ALL node invariants, including those that may have been
        # invalidated by later nodes, not only the final user acceptance.
        all_checks = [c for node in graph["nodes"] for c in node.get("checks", [])]
        final_checks = evaluate_all([*all_checks, *graph["acceptance"]], root)
        state["final_evidence"] = final_checks
        state["status"] = "SUCCEEDED" if all(x["ok"] for x in final_checks) else "BLOCKED"
        emit(run_dir, "final_verification", passed=state["status"] == "SUCCEEDED")
    state["updated_at"] = utc()
    save_json(run_dir / "state.json", state)
    emit(run_dir, "run_stopped", status=state["status"])
    return state
