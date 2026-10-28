"""Locked DAG validation and compile-time hierarchical flow expansion."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from .specs import digest

TYPES = {"agent", "model", "command", "atomic", "verify", "flow"}
IDENTIFIER = re.compile(r"^[a-zA-Z][\w-]*$")


def interpolate(obj, values: dict):
    if isinstance(obj, str):
        for key, val in values.items():
            if not re.fullmatch(r"[a-zA-Z][\w-]*", key) or not isinstance(val, (str, int, float, bool)):
                raise ValueError("Flow parameters must be scalar and safely named")
            obj = obj.replace("${" + key + "}", str(val))
        if re.search(r"\$\{[^}]+}", obj):
            raise ValueError(f"Unresolved parameter in {obj}")
        return obj
    if isinstance(obj, list):
        return [interpolate(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: interpolate(v, values) for k, v in obj.items()}
    return obj


def expand(raw: dict, base: Path, *, prefix: str = "", stack: tuple[Path, ...] = ()) -> list[dict]:
    """Compile nested flow into namespaced nodes; a flow dependency means all its leaves."""
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("A graph needs a nonempty nodes array")
    ids = [n.get("id") for n in nodes]
    if any(not isinstance(i, str) or not IDENTIFIER.fullmatch(i) for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("Node IDs must be unique simple identifiers")
    for n in nodes:
        if n.get("type") not in TYPES:
            raise ValueError(f"Unknown node type: {n.get('type')}")
        if not isinstance(n.get("needs", []), list) or any(d not in ids for d in n.get("needs", [])):
            raise ValueError(f"Unknown dependencies for {n['id']}")
    result = []
    roots = {}  # Flow node -> roots of its flattened subtree
    leaves = {} # Flow node -> leaves for references from downstream nodes
    for n in nodes:
        full_id = prefix + n["id"]
        if n["type"] == "flow":
            file = (base / n["file"]).resolve()
            if file in stack or len(stack) >= 12:
                raise ValueError("Recursive or excessively nested flow")
            expanded = expand(interpolate(json.loads(file.read_text()), n.get("with", {})), file.parent,
                              prefix=full_id + ".", stack=(*stack, file))
            roots[n["id"]] = [x["id"] for x in expanded if not x["needs"]]
            referenced = {need for x in expanded for need in x["needs"]}
            leaves[n["id"]] = [x["id"] for x in expanded if x["id"] not in referenced]
            result.extend(expanded)
        else:
            node = copy.deepcopy(n)
            node["id"] = full_id
            node["needs"] = []
            roots[n["id"]] = [full_id]
            leaves[n["id"]] = [full_id]
            result.append(node)
    for n in nodes:
        incoming = list(dict.fromkeys(dep for d in n.get("needs", []) for dep in leaves[d]))
        for node in result:
            if node["id"] in roots[n["id"]]:
                node["needs"] = list(dict.fromkeys([*node["needs"], *incoming]))
    order = topo(result)
    return order


def topo(nodes: list[dict]) -> list[dict]:
    by_id = {n["id"]: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("Duplicate expanded node ID")
    finished = set()
    ordered = []
    while len(finished) != len(nodes):
        runnable = [n for n in nodes if n["id"] not in finished and set(n.get("needs", [])).issubset(finished)]
        if not runnable:
            raise ValueError("Cycle or dangling dependency in DAG")
        for node in runnable:
            if any(d not in by_id for d in node.get("needs", [])):
                raise ValueError("Dangling dependency")
            finished.add(node["id"])
            ordered.append(node)
    return ordered


def validate(nodes: list[dict]) -> None:
    if not nodes:
        raise ValueError("Graph is empty")
    for n in nodes:
        kind = n["type"]
        if kind == "flow":
            raise ValueError("Flow not fully compiled")
        if kind in ("agent", "model") and not n.get("prompt"):
            raise ValueError(f"Agent node {n['id']} has no prompt")
        if kind == "atomic":
            from .atomic import OPS
            if n.get("op") not in OPS or not isinstance(n.get("args"), dict):
                raise ValueError(f"Atomic node {n['id']} requires registered op and args")
        if kind == "model" and (not n.get("output") or n.get("format", "text") not in ("text", "json")):
            raise ValueError(f"Model node {n['id']} requires output and text/json format")
        if kind == "command" and (not isinstance(n.get("argv"), list) or not n["argv"] or not all(isinstance(v, str) for v in n["argv"])):
            raise ValueError(f"Command node {n['id']} requires argv array")
        if kind == "verify" and not isinstance(n.get("checks"), list):
            raise ValueError(f"Verify node {n['id']} requires checks")
        if not isinstance(n.get("checks", []), list):
            raise ValueError("checks must be a list")
        if not isinstance(n.get("retries", 0), int) or not 0 <= n.get("retries", 0) <= 10:
            raise ValueError("retries must be 0-10")
    topo(nodes)


def graph_hash(graph: dict) -> str:
    return digest(json.dumps(graph, sort_keys=True, ensure_ascii=False))
