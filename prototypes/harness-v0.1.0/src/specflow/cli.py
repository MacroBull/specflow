"""`sf`: propose -> lock -> run -> verify with small diagnostic commands."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .agents import execute
from .engine import evaluate_all, run
from .graph import expand, validate, topo
from .specs import load_json, new_spec, parse_spec, save_json, slugify, digest


def cwd(args) -> Path:
    return Path(args.project).expanduser().resolve()


def spec_path(root: Path, change: str) -> Path:
    return root / ".specflow" / "specs" / f"{change}.md"


def locked_path(root: Path, change: str) -> Path:
    return root / ".specflow" / "locked" / f"{change}.json"


def ensure_change(value: str) -> str:
    if slugify(value) != value:
        raise ValueError("Change ID must be a lowercase ASCII slug (for example add-healthcheck)")
    return value


def propose(args) -> None:
    root = cwd(args)
    change = slugify(args.name or args.goal[:50])
    path = spec_path(root, change)
    if path.exists():
        raise ValueError(f"Spec already exists: {path} (edit it directly)")
    content = new_spec(args.name or change, args.goal, args.requirement, args.check, args.constraint)
    if args.agent:
        prompt = ("Draft a SpecFlow Markdown specification from the user goal. Output ONLY Markdown, "
                  "using exactly these sections: ## Goal, ## Requirements, ## Acceptance, ## Constraints. "
                  "Requirements are lines '- [R1] natural language'. Acceptance checks MUST cover "
                  "every Rn with lines '- [R1] file: relative/path', "
                  "'- [R1] contains: relative/path :: exact text' or "
                  "'- [R1] command: executable arguments'. Checks must be concretely executable, "
                  "not invented claims. Don't assume unknown build commands exist. "
                  "The user will review the spec before lock.\n\n" + content)
        content = execute(args.agent, prompt, root, profile="planner", model=args.model, auto_approve=args.auto_approve)
        parse_spec(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"PROPOSED {change}: {path}\nEdit/review the spec, then `sf lock {change} --approve`.")


def parse_llm_json(output: str) -> dict:
    value = output.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(value)


def lock(args) -> None:
    if not args.approve:
        raise ValueError("Review the natural-language spec first; lock requires --approve")
    root = cwd(args)
    change = ensure_change(args.change)
    spec_content = spec_path(root, change).read_text()
    spec = parse_spec(spec_content)
    if args.graph:
        file = Path(args.graph).resolve()
        raw = load_json(file)
        base = file.parent
    elif args.planner:
        prompt = ("Produce only valid JSON (no Markdown). Design an executable DAG for this SpecFlow change. "
                  "Use an object {\"nodes\":[...]}. Node types: agent with {id,type,prompt,profile?,needs?,checks?,retries?}, "
                  "model with {id,type,prompt,output,format?,checks?}, "
                  "atomic with {id,type,op,args,needs?,checks?}, "
                  "command with argv array, verify with checks array, flow with file and with parameters. "
                  "Every node ID must be unique; needs reference earlier IDs; use conservative, short DAG (1-5 nodes). "
                  "For agent nodes, prompt is concrete implementation work, not commentary. "
                  "The harness automatically appends immutable final acceptance checks. "
                  "Do NOT add a final verify node. The user can inspect the graph before running.\n\n" + spec_content)
        raw = parse_llm_json(execute(args.planner, prompt, root, profile="planner", model=args.model,
                                     auto_approve=args.auto_approve))
        base = root
    else:
        raw = {"nodes": [{"id": "implement", "type": "agent", "profile": "builder", "needs": [],
                          "prompt": f"Implement all requirements in the locked spec for '{change}' with minimal changes.",
                          "retries": 1}]}
        base = root
    compiled = expand(raw, base)
    validate(compiled)
    all_ids = {x["id"] for x in compiled}
    referenced = {dep for n in compiled for dep in n["needs"]}
    sinks = sorted(all_ids - referenced)
    compiled.append({"id": "sf-final-acceptance", "type": "verify", "needs": sinks,
                     "checks": spec["checks"]})
    compiled = topo(compiled)
    validate(compiled)
    graph = {"version": 1, "change": change, "spec_hash": spec["hash"],
             "requirements": spec["requirements"], "acceptance": spec["checks"], "nodes": compiled}
    dest = locked_path(root, change)
    if dest.exists() and not args.replace:
        raise ValueError(f"Lock already exists: {dest} (use --replace to explicitly supersede)")
    save_json(dest, graph)
    print(f"LOCKED {change}: {dest} ({len(compiled)} nodes; {len(spec['checks'])} final checks)")


def verify(args) -> None:
    root = cwd(args)
    change = ensure_change(args.change)
    graph = load_json(locked_path(root, change))
    if digest(spec_path(root, change).read_text()) != graph["spec_hash"]:
        raise ValueError("Spec hash differs from lock")
    report = evaluate_all([*(c for node in graph["nodes"] for c in node.get("checks", [])), *graph["acceptance"]], root)
    for check in report:
        print(f"{'PASS' if check['ok'] else 'FAIL'} [{check['check'].get('requirement', 'node')}]: {check['detail']}")
    if not all(c["ok"] for c in report):
        raise SystemExit(1)


def do_run(args) -> None:
    root = cwd(args)
    state = run(root, ensure_change(args.change), provider=args.agent, run_id=args.run_id,
                auto_approve=args.auto_approve, model=args.model)
    print(json.dumps({"run_id": state["run_id"], "status": state["status"],
                      "nodes": {k: v["status"] for k, v in state["nodes"].items()}}, indent=2))
    if state["status"] != "SUCCEEDED":
        raise SystemExit(1)


def do_status(args) -> None:
    root = cwd(args)
    runs = root / ".specflow" / "runs"
    if not runs.exists():
        print("No runs found")
        return
    for path in sorted(runs.iterdir(), reverse=True):
        state_file = path / "state.json"
        if state_file.is_file():
            state = load_json(state_file)
            print(f"{state['run_id']}  {state['change']}  {state['status']}  "
                  + ", ".join(f"{n}:{v['status']}" for n,v in state["nodes"].items()))


def retry(args) -> None:
    root = cwd(args)
    path = root / ".specflow" / "runs" / args.run_id / "state.json"
    state = load_json(path)
    if args.node_id not in state["nodes"]:
        raise ValueError("Unknown node ID")
    node = state["nodes"][args.node_id]
    if node["status"] not in ("RUNNING", "FAILED"):
        raise ValueError("Only interrupted or failed nodes can be retried")
    node["status"] = "PENDING"
    node.pop("error", None)
    state["status"] = "BLOCKED"
    save_json(path, state)
    print(f"Requeued {args.node_id}; resume: sf run {state['change']} --run-id {args.run_id}")


def hook_config(provider: str) -> dict:
    if provider == "codex":
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "sf hook stop --provider codex"}]}],
                          "SessionStart": [{"hooks": [{"type": "command", "command": "sf hook context --provider codex"}]}]}}
    if provider == "claude":
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "sf hook stop --provider claude"}]}],
                          "SessionStart": [{"hooks": [{"type": "command", "command": "sf hook context --provider claude"}]}]}}
    if provider == "copilot":
        return {"version": 1, "hooks": {"agentStop": [{"type": "command", "bash": "sf hook stop --provider copilot"}],
                                         "sessionStart": [{"type": "command", "bash": "sf hook context --provider copilot"}]}}
    raise ValueError("Unknown provider")


def hooks(args) -> None:
    if args.hook_action == "print":
        print(json.dumps(hook_config(args.provider), indent=2))
        return
    # Hook handlers should be safe outside managed sf run and produce no misleading claims.
    raw = os.environ.get("SPECFLOW_RUN_DIR")
    node_id = os.environ.get("SPECFLOW_NODE_ID")
    root_name = os.environ.get("SPECFLOW_PROJECT")
    if not (raw and node_id and root_name):
        print("{}")
        return
    state = load_json(Path(raw) / "state.json")
    graph = load_json(Path(root_name) / ".specflow" / "locked" / f"{state['change']}.json")
    node = next(n for n in graph["nodes"] if n["id"] == node_id)
    incoming = json.loads(__import__("sys").stdin.read() or "{}")
    if args.hook_action == "context":
        message = f"SpecFlow locked task {state['change']}, active node {node_id}. Follow the locked spec; Harness, not your response, determines completion."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}})
              if args.provider in ("codex", "claude") else json.dumps({"additionalContext": message}))
        return
    from .engine import evaluate_all
    failed = [x["detail"] for x in evaluate_all(node.get("checks", []), Path(root_name)) if not x["ok"]]
    # Avoid an infinite inner hook loop. The outer sf run handles further retries.
    if not failed or incoming.get("stop_hook_active", False):
        print("{}")
    else:
        print(json.dumps({"decision": "block", "reason": "SpecFlow node checks failed:\n" + "\n".join(failed) + "\nRepair before finishing."}))


def inspect(args) -> None:
    root = cwd(args)
    change = ensure_change(args.change)
    spec = parse_spec(spec_path(root, change).read_text())
    print(f"Goal: {spec['goal']}")
    print("Requirements:")
    for req, text in spec["requirements"].items():
        print(f"  {req}: {text}")
    print(f"Acceptance checks: {len(spec['checks'])}")
    file = locked_path(root, change)
    if file.exists():
        graph = load_json(file)
        print(f"Lock: {'CURRENT' if graph['spec_hash'] == spec['hash'] else 'STALE'}")
        for node in graph["nodes"]:
            print(f"  {node['id']} [{node['type']}], depends on: {', '.join(node['needs']) or '(root)'}")


def graph_view(args) -> None:
    graph = load_json(locked_path(cwd(args), ensure_change(args.change)))
    print("flowchart TD")
    for node in graph["nodes"]:
        name = node["id"]
        print(f'    "{name}"["{name} ({node["type"]})"]')
        for dep in node["needs"]:
            print(f'    "{dep}" --> "{name}"')


def trace(args) -> None:
    file = cwd(args) / ".specflow" / "runs" / args.run_id / "events.jsonl"
    for line in file.read_text().splitlines():
        event = json.loads(line)
        print(f"{event['time']} {event['event']} " + " ".join(f"{k}={v}" for k,v in event.items() if k not in ("time", "event")))


def doctor(args) -> None:
    import shutil
    root = cwd(args)
    print(f"Workspace: {root}")
    print(f"SpecFlow initialized: {(root / '.specflow').exists()}")
    for name in ("codex", "claude", "copilot"):
        print(f"{name}: {shutil.which(name) or 'NOT INSTALLED'}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sf", description="SpecFlow - locked agent DAG harness")
    p.add_argument("--project", default=".", help="Workspace root (default current directory)")
    sub = p.add_subparsers(dest="command", required=True)
    propose_p = sub.add_parser("propose", help="Create human-editable Markdown spec")
    propose_p.add_argument("goal")
    propose_p.add_argument("--name")
    propose_p.add_argument("--requirement", action="append", default=[])
    propose_p.add_argument("--check", action="append", default=[], help="e.g. '[R1] file: README.md'")
    propose_p.add_argument("--constraint", action="append", default=[])
    propose_p.add_argument("--agent", choices=["codex", "claude", "copilot"])
    propose_p.add_argument("--model")
    propose_p.add_argument("--auto-approve", action="store_true")
    propose_p.set_defaults(func=propose)
    lock_p = sub.add_parser("lock", help="Validate spec and freeze expanded DAG")
    lock_p.add_argument("change")
    lock_p.add_argument("--graph", help="Explicit graph JSON; otherwise minimal one-agent graph")
    lock_p.add_argument("--planner", choices=["codex", "claude", "copilot"])
    lock_p.add_argument("--model")
    lock_p.add_argument("--auto-approve", action="store_true")
    lock_p.add_argument("--approve", action="store_true", help="Human has reviewed acceptance contract")
    lock_p.add_argument("--replace", action="store_true")
    lock_p.set_defaults(func=lock)
    run_p = sub.add_parser("run", help="Execute a locked DAG with persisted node states")
    run_p.add_argument("change")
    run_p.add_argument("--agent", choices=["codex", "claude", "copilot"], default="codex")
    run_p.add_argument("--run-id")
    run_p.add_argument("--model")
    run_p.add_argument("--auto-approve", action="store_true")
    run_p.set_defaults(func=do_run)
    verify_p = sub.add_parser("verify", help="Re-run final acceptance contract")
    verify_p.add_argument("change")
    verify_p.set_defaults(func=verify)
    status_p = sub.add_parser("status", help="List local runs")
    status_p.set_defaults(func=do_status)
    retry_p = sub.add_parser("retry", help="Explicitly requeue an interrupted/failed node")
    retry_p.add_argument("run_id")
    retry_p.add_argument("node_id")
    retry_p.set_defaults(func=retry)
    hooks_p = sub.add_parser("hook", help="Generate / service native lifecycle hooks")
    hooks_p.add_argument("hook_action", choices=["print", "stop", "context"])
    hooks_p.add_argument("--provider", choices=["codex", "claude", "copilot"], required=True)
    hooks_p.set_defaults(func=hooks)
    inspect_p = sub.add_parser("inspect", help="Explain requirements, checks and locked nodes")
    inspect_p.add_argument("change")
    inspect_p.set_defaults(func=inspect)
    graph_p = sub.add_parser("graph", help="Render DAG as Mermaid")
    graph_p.add_argument("change")
    graph_p.set_defaults(func=graph_view)
    trace_p = sub.add_parser("trace", help="Print chronological JSONL event history")
    trace_p.add_argument("run_id")
    trace_p.set_defaults(func=trace)
    doctor_p = sub.add_parser("doctor", help="Check adapters and workspace")
    doctor_p.set_defaults(func=doctor)
    return p


def main() -> None:
    try:
        args = parser().parse_args()
        args.func(args)
    except (ValueError, FileNotFoundError, RuntimeError, json.JSONDecodeError) as error:
        import sys
        print(f"sf: {error}", file=sys.stderr)
        raise SystemExit(2) from error

if __name__ == "__main__":
    main()
