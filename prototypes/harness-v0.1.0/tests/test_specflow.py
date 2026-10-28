import json
from pathlib import Path
import pytest
from specflow.cli import parser
from specflow.specs import new_spec, parse_spec, save_json
from specflow.graph import expand, validate
from specflow.engine import run, evaluate_all


def test_spec_coverage():
    content = new_spec("Test", "Produce greeting", ["greeting exists", "it is valid"],
                       ["[R1] file: out.txt"], [])
    with pytest.raises(ValueError, match="Missing acceptance"):
        parse_spec(content)
    spec = parse_spec(content.replace("## Constraints", "- [R2] contains: out.txt :: hi\n\n## Constraints"))
    assert len(spec["checks"]) == 2


def test_nested_graph_topology(tmp_path):
    flow = tmp_path / "sub.json"
    save_json(flow, {"nodes": [{"id": "create", "type": "command", "argv": ["echo", "${message}"]},
                               {"id": "check", "type": "verify", "needs": ["create"], "checks": []}]})
    raw = {"nodes": [{"id": "setup", "type": "command", "argv": ["echo", "setup"]},
                     {"id": "sub", "type": "flow", "file": "sub.json", "with": {"message": "hello"}, "needs": ["setup"]},
                     {"id": "finish", "type": "verify", "needs": ["sub"], "checks": []}]}
    compiled = expand(raw, tmp_path)
    validate(compiled)
    assert [n["id"] for n in compiled] == ["setup", "sub.create", "sub.check", "finish"]
    assert compiled[1]["needs"] == ["setup"]
    assert compiled[-1]["needs"] == ["sub.check"]
    assert compiled[1]["argv"][-1] == "hello"


def test_cycle_and_duplicate(tmp_path):
    with pytest.raises(ValueError, match="Cycle"):
        expand({"nodes": [{"id": "a", "type": "verify", "needs": ["b"]},
                          {"id": "b", "type": "verify", "needs": ["a"]}]}, tmp_path)
    with pytest.raises(ValueError, match="unique"):
        expand({"nodes": [{"id": "a", "type": "verify"}, {"id": "a", "type": "verify"}]}, tmp_path)


def setup_workspace(tmp_path):
    sf = tmp_path / ".specflow"
    content = new_spec("Test", "Write greeting", ["out contains hello"],
                       ["[R1] contains: greeting.txt :: hello"], [])
    (sf / "specs").mkdir(parents=True)
    (sf / "specs" / "demo.md").write_text(content)
    checks = parse_spec(content)["checks"]
    graph = {"version": 1, "change": "demo", "spec_hash": parse_spec(content)["hash"],
             "requirements": {"R1": "out contains hello"}, "acceptance": checks,
             "nodes": [{"id": "write", "type": "command", "needs": [],
                        "argv": ["python", "-c", "from pathlib import Path; Path('greeting.txt').write_text('hello')"]},
                       {"id": "sf-final-acceptance", "type": "verify", "needs": ["write"], "checks": checks}]}
    save_json(sf / "locked" / "demo.json", graph)
    return sf


def test_e2e_command_graph_and_resume(tmp_path):
    setup_workspace(tmp_path)
    result = run(tmp_path, "demo", provider="codex")
    assert result["status"] == "SUCCEEDED"
    assert (tmp_path / "greeting.txt").read_text() == "hello"
    assert run(tmp_path, "demo", provider="codex", run_id=result["run_id"])["status"] == "SUCCEEDED"
    assert len(list((tmp_path / ".specflow" / "runs").iterdir())) == 1


def test_incomplete_does_not_succeed(tmp_path):
    sf = setup_workspace(tmp_path)
    graph = json.loads((sf / "locked" / "demo.json").read_text())
    graph["nodes"][0]["argv"] = ["python", "-c", "print('no artifact')"]
    save_json(sf / "locked" / "demo.json", graph)
    result = run(tmp_path, "demo", provider="codex")
    assert result["status"] == "BLOCKED"
    assert result["nodes"]["sf-final-acceptance"]["status"] == "FAILED"


def test_locked_spec_is_immutable(tmp_path):
    sf = setup_workspace(tmp_path)
    (sf / "specs" / "demo.md").write_text("tampered")
    with pytest.raises(ValueError, match="Locked spec changed"):
        run(tmp_path, "demo", provider="codex")


def test_check_cannot_escape_workspace(tmp_path):
    report = evaluate_all([{"type": "file", "path": "../secrets.txt"}], tmp_path)
    assert not report[0]["ok"]
    assert "escapes" in report[0]["detail"]


def test_interrupted_node_requires_explicit_retry(tmp_path):
    from specflow.engine import init_run
    sf = setup_workspace(tmp_path)
    graph = json.loads((sf / "locked" / "demo.json").read_text())
    directory, state = init_run(tmp_path, "demo", graph)
    state["nodes"]["write"]["status"] = "RUNNING"
    save_json(directory / "state.json", state)
    with pytest.raises(RuntimeError, match="Interrupted"):
        run(tmp_path, "demo", provider="codex", run_id=state["run_id"])


def test_final_revalidates_intermediate_invariants(tmp_path):
    sf = setup_workspace(tmp_path)
    graph = json.loads((sf / "locked" / "demo.json").read_text())
    graph["nodes"][0]["checks"] = [{"type": "contains", "path": "greeting.txt", "text": "hello"}]
    # A later stage damages an intermediate invariant, then rewrites the user acceptance.
    graph["nodes"].insert(1, {"id": "damage", "type": "command", "needs": ["write"],
                             "argv": ["python", "-c", "from pathlib import Path; Path('greeting.txt').write_text('HELLO')"]})
    graph["nodes"][-1]["needs"] = ["damage"]
    graph["acceptance"] = [{"type": "file", "path": "greeting.txt", "requirement": "R1"}]
    graph["nodes"][-1]["checks"] = graph["acceptance"]
    save_json(sf / "locked" / "demo.json", graph)
    result = run(tmp_path, "demo", provider="codex")
    assert result["nodes"]["sf-final-acceptance"]["status"] == "SUCCEEDED"
    assert result["status"] == "BLOCKED"
    assert any(not x["ok"] for x in result["final_evidence"])


def test_hook_stop_continuation(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from specflow.cli import hooks
    sf = setup_workspace(tmp_path)
    from specflow.engine import init_run
    graph = json.loads((sf / "locked" / "demo.json").read_text())
    run_dir, _ = init_run(tmp_path, "demo", graph)
    monkeypatch.setenv("SPECFLOW_PROJECT", str(tmp_path))
    monkeypatch.setenv("SPECFLOW_RUN_DIR", str(run_dir))
    monkeypatch.setenv("SPECFLOW_NODE_ID", "sf-final-acceptance")
    import io, sys
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"stop_hook_active": false}'))
    hooks(Namespace(hook_action="stop", provider="claude"))
    assert '"block"' in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"stop_hook_active": true}'))
    hooks(Namespace(hook_action="stop", provider="claude"))
    assert capsys.readouterr().out.strip() == "{}"


def test_atomic_registry(tmp_path):
    from specflow.atomic import dispatch
    dispatch(tmp_path, "json.write@1", {"path": "a.json", "data": {"answer": 42}})
    assert json.loads((tmp_path / "a.json").read_text())["answer"] == 42
    dispatch(tmp_path, "fs.copy@1", {"source": "a.json", "target": "copy.json"})
    assert (tmp_path / "copy.json").read_bytes() == (tmp_path / "a.json").read_bytes()
    with pytest.raises(ValueError):
        dispatch(tmp_path, "text.write@1", {"path": "../escape", "text": "no"})


def test_model_node_stores_structured_result(tmp_path, monkeypatch):
    import specflow.engine as engine
    sf = setup_workspace(tmp_path)
    run_dir = sf / "runs" / "manual"
    run_dir.mkdir(parents=True)
    save_json(run_dir / "state.json", {"change": "demo"})
    monkeypatch.setattr(engine, "execute", lambda *args, **kwargs: '{"ok": true, "value": 3}')
    output = engine.execute_node({"id": "analyze", "type": "model", "prompt": "Summarize", "output": "report.json", "format": "json"},
                                 tmp_path, "codex", run_dir, "", auto_approve=False)
    assert json.loads(output)["value"] == 3
    assert json.loads((tmp_path / "report.json").read_text())["ok"] is True
