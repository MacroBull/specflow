"""Tiny deterministic atomic library. Upgrade agent nodes by swapping implementation."""
from __future__ import annotations
import json
import shutil
from pathlib import Path


def inside(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Atomic path escapes workspace: {rel}")
    return path


def text_write(root: Path, args: dict) -> str:
    path = inside(root, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["text"])
    return str(path)


def json_write(root: Path, args: dict) -> str:
    return text_write(root, {"path": args["path"], "text": json.dumps(args["data"], indent=2, ensure_ascii=False) + "\n"})


def file_copy(root: Path, args: dict) -> str:
    source, target = inside(root, args["source"]), inside(root, args["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return str(target)


def dir_make(root: Path, args: dict) -> str:
    path = inside(root, args["path"])
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


OPS = {"text.write@1": text_write, "json.write@1": json_write,
       "fs.copy@1": file_copy, "fs.mkdir@1": dir_make}


def dispatch(root: Path, op: str, args: dict) -> str:
    if op not in OPS:
        raise ValueError(f"Unknown atomic op: {op}")
    if not isinstance(args, dict):
        raise ValueError("Atomic arguments must be an object")
    return OPS[op](root, args)
