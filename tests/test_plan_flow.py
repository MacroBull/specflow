from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from specflow import engine


class PlanFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = Path.cwd()
        os.chdir(self.root)

        control = self.root / ".specflow"
        engine.ROOT = control
        engine.FLOW = control / "flow.json"
        engine.PLAN = control / "plan.json"
        engine.DRAFT = control / "draft.json"
        engine.SPEC = control / "SPEC.md"
        engine.STATE = control / "state.json"

        (self.root / "src").mkdir()
        (self.root / "src" / "__init__.py").write_text("")
        (self.root / "src" / "parser.py").write_text(
            'def parse_csv(line: str) -> list[str]:\n'
            '    return [part for part in line.split(",") if part]\n'
        )
        plan = {
            "name": "planner",
            "objective": "Produce spec and verifier",
            "instructions": ["Do not implement"],
            "tools": ["read"],
        }
        (self.root / "plan.json").write_text(json.dumps(plan))

    def tearDown(self) -> None:
        os.chdir(self.previous)
        self.temp.cleanup()

    def proposal(self) -> tuple[str, dict, list[dict[str, str]]]:
        spec = "Preserve empty fields."
        flow = {
            "name": "demo",
            "initial": "red",
            "stages": {
                "red": {
                    "objective": "reproduce",
                    "tools": ["read"],
                    "verify": ["python", "{verifier_root}/verify.py", "red"],
                    "next": "green",
                },
                "green": {
                    "objective": "fix",
                    "tools": ["read", "write"],
                    "write_paths": ["src"],
                    "verify": ["python", "{verifier_root}/verify.py", "green"],
                    "next": "completed",
                },
            },
        }
        verifier = '''import sys
from src.parser import parse_csv
expected = ["a", "", "b", ""]
actual = parse_csv("a,,b,")
if sys.argv[1] == "red":
    raise SystemExit(0 if actual != expected else 1)
raise SystemExit(0 if actual == expected else 1)
'''
        return spec, flow, [{"path": "verify.py", "content": verifier}]

    def test_plan_discussion_approval_and_automatic_flow(self) -> None:
        state = engine.start_plan("plan.json", "Preserve empty fields")
        self.assertEqual(state["status"], "planning")
        self.assertEqual(engine.policy("write", {"path": "src/parser.py"})["permissionDecision"], "deny")

        engine.record_human_message("Include consecutive and trailing empty fields")
        self.assertIn("trailing", engine.view()["latest_feedback"])

        spec, flow, verifiers = self.proposal()
        proposed = engine.propose_flow(spec, flow, verifiers)
        self.assertTrue(proposed["accepted"])
        self.assertEqual(engine.stop()["decision"], "block")

        approved = engine.approve(True, "approved")
        self.assertEqual(approved["stage"], "red")
        self.assertTrue(engine.SPEC.exists())
        self.assertNotIn("{verifier_root}", json.dumps(json.loads(engine.FLOW.read_text())))

        red = engine.stop()
        self.assertEqual(red["stage"], "green")

        (self.root / "src" / "parser.py").write_text(
            'def parse_csv(line: str) -> list[str]:\n'
            '    return line.split(",")\n'
        )
        done = engine.stop()
        self.assertTrue(done["completed"])
        self.assertEqual(engine.load_state()["status"], "completed")

    def test_model_change_request_returns_to_human_planning(self) -> None:
        engine.start_plan("plan.json", "Goal")
        spec, flow, verifiers = self.proposal()
        engine.propose_flow(spec, flow, verifiers)
        engine.approve(True)

        engine.request_change("Verifier misses an invariant", "Add another check")
        self.assertEqual(engine.load_state()["status"], "human_pending")
        engine.begin_replan("Add the invariant")
        current = engine.view()
        self.assertEqual(current["status"], "planning")
        self.assertEqual(current["latest_feedback"], "Add the invariant")


if __name__ == "__main__":
    unittest.main()
