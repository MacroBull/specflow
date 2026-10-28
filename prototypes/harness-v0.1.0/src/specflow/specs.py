"""Human-editable spec format. No YAML parser, no magic natural-language interpretation."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

SECTIONS = ("Goal", "Requirements", "Acceptance", "Constraints")
CHECKS = {"file", "contains", "command"}


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("Name must contain ASCII letters or digits")
    return slug[:64]


def new_spec(name: str, goal: str, requirements: list[str], checks: list[str], constraints: list[str]) -> str:
    reqs = requirements or [goal]
    return (f"# {name}\n\n## Goal\n{goal.strip()}\n\n"
            + "## Requirements\n" + "\n".join(f"- [R{i}] {v}" for i, v in enumerate(reqs, 1))
            + "\n\n## Acceptance\n" + ("\n".join(f"- {x}" for x in checks) if checks else "<!-- Add observable checks: - [R1] file: out.txt -->")
            + "\n\n## Constraints\n" + ("\n".join(f"- {v}" for v in constraints) if constraints else "- Stay inside the project workspace") + "\n")


def parse_spec(content: str) -> dict:
    sections: dict[str, str] = {}
    for m in re.finditer(r"^## (Goal|Requirements|Acceptance|Constraints)\s*$", content, re.M):
        nxt = re.search(r"^## ", content[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(content)
        if m.group(1) in sections:
            raise ValueError(f"Duplicate section: {m.group(1)}")
        sections[m.group(1)] = content[m.end():end].strip()
    if any(s not in sections for s in SECTIONS):
        raise ValueError("Spec must have Goal, Requirements, Acceptance and Constraints sections")
    goal = sections["Goal"]
    if not goal:
        raise ValueError("Goal cannot be empty")
    requirements = {}
    for line in sections["Requirements"].splitlines():
        if not line.strip() or line.startswith("<!--"):
            continue
        m = re.fullmatch(r"\s*- \[(R\d+)\] (.+?)\s*", line)
        if not m:
            raise ValueError(f"Invalid requirement: {line}")
        if m[1] in requirements:
            raise ValueError(f"Duplicate requirement: {m[1]}")
        requirements[m[1]] = m[2]
    if not requirements:
        raise ValueError("At least one requirement needed")
    acceptance = []
    for line in sections["Acceptance"].splitlines():
        if not line.strip() or line.startswith("<!--"):
            continue
        m = re.fullmatch(r"\s*- \[(R\d+)\] (file|contains|command):\s*(.+?)\s*", line)
        if not m:
            raise ValueError(f"Invalid acceptance: {line}")
        req, kind, value = m.groups()
        if req not in requirements:
            raise ValueError(f"Check references unknown requirement {req}")
        if kind == "contains":
            # `contains: relative/file.txt :: required literal` avoids expressions/eval.
            if " :: " not in value:
                raise ValueError("contains check format: PATH :: LITERAL")
            path, needle = value.split(" :: ", 1)
            check = {"type": kind, "path": path, "text": needle}
        elif kind == "file":
            check = {"type": kind, "path": value}
        else:
            import shlex
            argv = shlex.split(value)
            if not argv:
                raise ValueError("Empty command check")
            check = {"type": kind, "argv": argv}
        check["requirement"] = req
        acceptance.append(check)
    uncovered = set(requirements) - {c["requirement"] for c in acceptance}
    if uncovered:
        raise ValueError(f"Missing acceptance checks for {', '.join(sorted(uncovered))}")
    return {"goal": goal, "requirements": requirements, "checks": acceptance,
            "constraints": sections["Constraints"], "hash": digest(content)}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
