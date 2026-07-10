# specflow

**Tool-gated spec & flow.**

Specflow is a hook-first harness, not a user-facing CLI.

- **tool-in-control**: hooks, typed MCP tools and verifiers own permissions and transitions;
- **model-in-loop**: the model plans or solves only the current mode/stage;
- **human-on-change**: humans approve generated specs and any later workflow revision;
- approved stages advance automatically when their frozen verifier passes;
- models can request a change but cannot approve one.

```text
UserPromptSubmit "%plan ..."
  → hook enters planning before the model
  → MCP exposes planning state + propose_flow schema
  → human and model discuss spec/verifier coverage
  → model submits structured spec + flow + verifier files
  → human %approve freezes the proposal
  → optional subagent runs UserPromptSubmit–Stop loops
  → PreToolUse enforces current-stage capabilities
  → Stop runs the frozen verifier
      pass → next stage automatically
      fail → continue current stage
      change needed → MCP request_change → human revises/re-approves
```

## Model-visible MCP surface

The model is not taught the complete state machine through a long prompt. `tools/list` changes with authoritative state:

### Planning

- `specflow.current_stage`
- `specflow.propose_flow`

`propose_flow` uses a strict schema for:

- Markdown spec;
- staged flow;
- stage-specific capabilities;
- executable verifier files.

### Running

- `specflow.current_stage`
- `specflow.request_change`

Approval and amendment are intentionally absent from MCP. They are human host actions received through `UserPromptSubmit`:

```text
%plan <goal>       start a plan discussion
%approve [note]    freeze the current proposal and enter/resume the flow
%reject <feedback> reject the current proposal but remain in planning
%revise <feedback> reopen planning after a model escalation
%status            inspect authoritative state
```

## Install

```bash
pip install -e .
```

Runtime entry points used by agent configuration:

```text
specflow-hook   # lifecycle-hook adapter; reads hook JSON on stdin
specflow-mcp    # stdio MCP server
```

There is deliberately no interactive `specflow` command.

## Hook mapping

Set `SPECFLOW_EVENT` for the shared hook adapter:

```text
UserPromptSubmit → SPECFLOW_EVENT=UserPromptSubmit specflow-hook
PreToolUse       → SPECFLOW_EVENT=PreToolUse specflow-hook
Stop             → SPECFLOW_EVENT=Stop specflow-hook
```

For planning, set `SPECFLOW_PLAN` to a planner policy JSON. The default is `.specflow/tools/plan.json`.

The full submit-to-stop loop may run inside a routed subagent. `current_stage` carries only the current goal, latest human feedback, instructions, tools and schemas, avoiding the parent transcript and full workflow in each model turn.

## Plan-first demo

[`examples/plan-demo`](examples/plan-demo) demonstrates:

```text
human goal
  → plan discussion
  → structured proposal
  → human approval
  → frozen verifier
  → red → green → review → completed
```

The example includes a planner policy, buggy code, an example `propose_flow` payload and a full conversation walkthrough.

## Security boundary

Approved verifier files are content-addressed under `.specflow/verifiers/<digest>/`. The model cannot write control files through the governed `write` tool. Production deployments should additionally protect `.specflow` at the sandbox/filesystem layer so alternate tools cannot bypass the hook policy.
