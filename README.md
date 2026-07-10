# specflow

**Tool-gated spec & flow.**

Specflow is a hook-first harness, not a user-facing CLI.

- **tool-in-control**: hooks and verifiers own transitions and permissions;
- **model-in-loop**: the model solves only the current approved stage;
- **human-on-change**: humans approve the initial spec and any later workflow change;
- normal verified stages advance automatically;
- models can raise `specflow.request_change` instead of bypassing the flow.

```text
UserPromptSubmit "%tdd ..."
  → tool router selects the flow
  → hook injects only current-stage instructions
  → optional subagent runs the UserPromptSubmit–Stop loop
  → PreToolUse enforces stage capabilities
  → Stop runs the verifier
      pass → next stage automatically
      fail → current stage continues
      change needed → model calls MCP request_change → human decision
```

## Why MCP

The model is not taught the complete state machine in a prompt. The MCP server exposes only:

- `specflow.current_stage`: current objective, instructions, tools and JSON schemas;
- `specflow.request_change`: pause and request a human-approved workflow revision.

Stage-specific schemas support structured decoding and reduce invalid tool retries. Different routed tools can select different instructions, subagents, hooks and harness policies.

## Install

```bash
pip install -e .
```

Runtime entry points used by agent configuration:

```text
specflow-hook   # lifecycle-hook adapter, reads JSON on stdin
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

A prompt beginning with `%tdd` selects `.specflow/tools/tdd.json` by default. `SPECFLOW_FLOW` can override routing. The hook may be configured on a subagent so its entire submit-to-stop loop is governed without polluting the parent agent context.

## Demo

`examples/tdd-demo/flow.json` contains three automatically advancing stages:

```text
red → green → review → completed
```

The initial spec must be approved by the host/human control surface. During execution, the model reads its current contract through MCP, uses only exposed stage tools, and calls `request_change` when the approved flow is insufficient.

## Security boundary

Workflow approval and amendment are host operations, intentionally absent from model-visible MCP tools. The model can request a change, but cannot approve or install one.
