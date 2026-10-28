#!/usr/bin/env python3
"""SpecFlow (sf) — Minimalist, Deterministic Agent Harness Core Engine."""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

SF_DIR = Path(".specflow")
SPECS_DIR = SF_DIR / "specs"
LOCKS_DIR = SF_DIR / "locks"
RUNS_DIR = SF_DIR / "runs"
TOOLS_DIR = SF_DIR / "tools"
ACTIVE_FILE = SF_DIR / "active_run.json"


@dataclass
class Profile:
    model: str = "default"
    isolation: str = "shared"  # shared | context_clean | worktree
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Edit", "Write", "Bash"])
    writable_paths: list[str] = field(default_factory=lambda: ["**"])
    denied_paths: list[str] = field(default_factory=list)
    max_turns: int = 15


@dataclass
class Node:
    id: str
    kind: str  # atomic | prompt | tool | flow
    depends_on: list[str] = field(default_factory=list)
    uses: str | None = None
    profile: str = "default"
    prompt: str = ""
    cmd: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    asserts: list[dict[str, Any]] = field(default_factory=list)


def resolve(val: Any, ctx: dict[str, Any]) -> Any:
    """Deterministic variable interpolation for ${RUN_ID}, ${inputs.*}, ${outputs.*}, ${nodes.*.outputs.*}."""
    if isinstance(val, str):
        val = val.replace("${RUN_ID}", ctx.get("run_id", "local"))
        for scope in ("inputs", "outputs"):
            for k, v in ctx.get(scope, {}).items():
                val = val.replace(f"${{{scope}.{k}}}", str(v))
        for nid, outs in ctx.get("node_outputs", {}).items():
            for k, v in outs.items():
                val = val.replace(f"${{nodes.{nid}.outputs.{k}}}", str(v))
        return val
    if isinstance(val, list):
        return [resolve(x, ctx) for x in val]
    if isinstance(val, dict):
        return {k: resolve(v, ctx) for k, v in val.items()}
    return val


def run_atomic(node: Node, ctx: dict[str, Any]) -> tuple[bool, str]:
    """Executes built-in std/* atomic nodes without LLM overhead."""
    inputs = resolve(node.inputs, ctx)
    outputs = resolve(node.outputs, ctx)
    if node.uses == "std/repo-map":
        summary: dict[str, list[str]] = {}
        for p in inputs.get("paths", ["."]):
            base = Path(p)
            if base.exists():
                summary[p] = sorted(
                    str(f) for f in base.rglob("*")
                    if f.is_file() and not any(part.startswith(".") for part in f.parts)
                )
        out_path = Path(outputs["repo_summary"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"files": summary}, indent=2), encoding="utf-8")
        return True, f"Wrote repo map to {out_path}"
    if node.uses == "std/cmd-exec":
        res = subprocess.run(inputs["cmd"], shell=True, capture_output=True, text=True)
        return (res.returncode == 0), (res.stdout or res.stderr).strip()
    if node.uses == "std/git-checkpoint":
        tag = f"sf-ckpt-{ctx['run_id']}-{node.id}"
        subprocess.run(["git", "tag", "-f", tag], capture_output=True)
        return True, f"Created checkpoint {tag}"
    return False, f"Unknown atomic primitive: {node.uses}"


def check_assertions(node: Node, ctx: dict[str, Any]) -> list[str]:
    """Evaluates node exit invariants. Empty list means all assertions passed."""
    failures: list[str] = []
    for rule in resolve(node.asserts, ctx):
        rtype = rule["type"]
        if rtype == "file_exists" and not Path(rule["path"]).exists():
            failures.append(f"[file_exists] Missing artifact: {rule['path']}")
        elif rtype == "cmd_exit_zero":
            res = subprocess.run(rule["cmd"], shell=True, capture_output=True, text=True)
            if res.returncode != 0:
                failures.append(f"[cmd_exit_zero] `{rule['cmd']}` failed ({res.returncode}): {(res.stderr or res.stdout)[-300:]}")
        elif rtype == "json_schema":
            p = Path(rule["path"])
            if not p.exists():
                failures.append(f"[json_schema] Missing JSON file: {p}")
            else:
                data = json.loads(p.read_text(encoding="utf-8"))
                missing = [k for k in rule.get("required_keys", []) if k not in data]
                if missing:
                    failures.append(f"[json_schema] {p} missing keys: {missing}")
        elif rtype == "no_git_diff_outside":
            res = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True)
            changed = [f.strip() for f in res.stdout.splitlines() if f.strip()]
            illegal = [f for f in changed if not any(fnmatch.fnmatch(f, pat) for pat in rule["allowed"])]
            if illegal:
                failures.append(f"[no_git_diff_outside] Unauthorized edits: {illegal}")
    return failures


def inject_host_prompt(host: str, dag_id: str, node: Node, profile: Profile, ctx: dict[str, Any], failures: list[str]) -> None:
    """Synchronizes active node contract into Claude Code, Codex, or Copilot instruction files."""
    target = {
        "claudecode": Path(".claude/CLAUDE.md"),
        "codex": Path("AGENTS.md"),
        "copilot": Path(".github/copilot-instructions.md"),
    }.get(host, SF_DIR / "active_context.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    block = [
        "<!-- SPECFLOW_START -->",
        f"# [SpecFlow Active Node] `{dag_id}` :: `{node.id}` (kind={node.kind}, isolation={profile.isolation})",
        f"- **Allowed Tools**: `{profile.allowed_tools}` | **Writable Paths**: `{resolve(profile.writable_paths, ctx)}`",
        f"- **Inputs**: `{json.dumps(resolve(node.inputs, ctx))}`",
        f"- **Required Outputs**: `{json.dumps(resolve(node.outputs, ctx))}`",
        f"\n## Task Instructions\n{resolve(node.prompt, ctx).strip()}",
    ]
    if failures:
        block.append("\n## [BLOCKED] Fix Failed Exit Assertions Before Stopping:\n" + "\n".join(f"- {e}" for e in failures))
    block.append("<!-- SPECFLOW_END -->")
    target.write_text("\n".join(block) + "\n", encoding="utf-8")


def cmd_env_init(args: argparse.Namespace) -> int:
    for d in (SPECS_DIR, LOCKS_DIR, RUNS_DIR, TOOLS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    (SF_DIR / "config.yaml").write_text(yaml.safe_dump({"host": args.host, "version": "1.0"}), encoding="utf-8")
    if args.host == "claudecode":
        Path(".claude").mkdir(exist_ok=True)
        hooks = {
            "hooks": {
                "PreToolUse": [{"matcher": "Edit|Write|Bash", "hooks": [{"type": "command", "command": "python3 sf.py hook pre-tool"}]}],
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 sf.py hook stop"}]}],
            }
        }
        Path(".claude/settings.json").write_text(json.dumps(hooks, indent=2), encoding="utf-8")
    print(f"[SpecFlow] Initialized .specflow/ for host={args.host}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    spec_file = SPECS_DIR / f"{args.name}.spec.md"
    spec_file.write_text(
        f"# SpecFlow Specification: {args.name}\n\n"
        f"## 1. Goal & Scope\n{args.intent}\n\n"
        f"## 2. Artifacts & Contracts\n- Target modules & JSON schemas\n\n"
        f"## 3. Acceptance Criteria\n- Unit tests pass and diff stays within scoped paths\n",
        encoding="utf-8",
    )
    print(f"[SpecFlow] Generated spec -> {spec_file}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec_file)
    digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()[:16]
    slug = spec_path.stem.replace(".spec", "")
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    dag_file = LOCKS_DIR / f"{slug}.dag.yaml"
    dag = {
        "version": "1.0",
        "id": slug,
        "spec_hash": f"sha256:{digest}",
        "profiles": {
            "architect_readonly": {
                "model": "claude-3-7-sonnet",
                "isolation": "context_clean",
                "allowed_tools": ["Read", "Grep", "Glob"],
                "writable_paths": [".specflow/runs/${RUN_ID}/artifacts/**"],
            },
            "coder_scoped": {
                "model": "claude-3-7-sonnet",
                "isolation": "shared",
                "allowed_tools": ["Read", "Edit", "Write", "Bash"],
                "writable_paths": ["src/**", "tests/**", ".specflow/runs/${RUN_ID}/artifacts/**"],
            },
        },
        "nodes": [
            {
                "id": "scan_context",
                "kind": "atomic",
                "uses": "std/repo-map",
                "inputs": {"paths": ["."]},
                "outputs": {"repo_summary": ".specflow/runs/${RUN_ID}/artifacts/repo_map.json"},
                "assert": [{"type": "file_exists", "path": "${outputs.repo_summary}"}],
            },
            {
                "id": "design_contract",
                "kind": "prompt",
                "depends_on": ["scan_context"],
                "profile": "architect_readonly",
                "inputs": {"map": "${nodes.scan_context.outputs.repo_summary}"},
                "prompt": "Design interface contract in ${outputs.contract} based on ${inputs.map}.",
                "outputs": {"contract": ".specflow/runs/${RUN_ID}/artifacts/contract.json"},
                "assert": [{"type": "json_schema", "path": "${outputs.contract}", "required_keys": ["interfaces"]}],
            },
        ],
    }
    dag_file.write_text(yaml.safe_dump(dag, sort_keys=False), encoding="utf-8")
    print(f"[SpecFlow] Locked DAG -> {dag_file} (sha256:{digest})")
    return 0


def cmd_upgrade_node(args: argparse.Namespace) -> int:
    dag_path = Path(args.dag_file)
    raw = yaml.safe_load(dag_path.read_text(encoding="utf-8"))
    node = next(n for n in raw["nodes"] if n["id"] == args.node_id)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    script = TOOLS_DIR / f"{args.node_id}.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\n"
        "outputs = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}\n"
        "for p in outputs.values():\n"
        "    Path(p).parent.mkdir(parents=True, exist_ok=True)\n"
        "    Path(p).write_text(json.dumps({'interfaces': []}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    node["kind"] = "tool"
    node["fallback_profile"] = node.pop("profile", "default")
    node["cmd"] = f"python3 {script} '${{inputs}}' '${{outputs}}'"
    dag_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"[SpecFlow] Upgraded node '{args.node_id}' -> tool script {script}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="sf", description="SpecFlow Agent Harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("env-init")
    p_init.add_argument("--host", default="claudecode", choices=["claudecode", "codex", "copilot"])
    p_init.set_defaults(func=cmd_env_init)

    p_prop = sub.add_parser("propose")
    p_prop.add_argument("intent")
    p_prop.add_argument("-n", "--name", default="feature")
    p_prop.set_defaults(func=cmd_propose)

    p_lock = sub.add_parser("lock")
    p_lock.add_argument("spec_file")
    p_lock.set_defaults(func=cmd_lock)

    p_upg = sub.add_parser("upgrade-node")
    p_upg.add_argument("dag_file")
    p_upg.add_argument("node_id")
    p_upg.set_defaults(func=cmd_upgrade_node)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
