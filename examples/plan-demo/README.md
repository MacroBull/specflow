# Plan-first demo

This demo starts with a human/model discussion. No execution flow exists until the human approves the generated spec and verifiers.

Run the agent from this directory with:

```text
SPECFLOW_PLAN=plan.json
SPECFLOW_ROOT=.specflow
```

Configure the three lifecycle hooks and the stdio MCP server described in the repository README.

## Conversation

### 1. Human starts planning

```text
%plan Preserve empty CSV fields in parse_csv.
```

`UserPromptSubmit` runs before the model. Specflow enters `planning`, exposes only planning tools, and injects a short planning objective.

The model may inspect `src/parser.py` and `tests/`, then discuss scope with the human.

```text
Human: Include consecutive and trailing empty fields. Do not add quoted CSV support.
```

Every human message in planning is recorded as the latest authoritative feedback, so a routed subagent can recover it through `specflow.current_stage` without receiving the full parent transcript.

### 2. Model proposes an executable contract

The model calls `specflow.propose_flow` using the structured arguments in [`proposal.json`](proposal.json):

- Markdown behavior spec;
- `red → green → review` state machine;
- stage-specific tools and write paths;
- verifier source files;
- `{verifier_root}` placeholders for immutable approved verifier paths.

The proposal is stored, but execution remains blocked.

The human can continue discussing and the model can submit another revision. Nothing runs until:

```text
%approve Covers consecutive/trailing empty fields; quoting stays out of scope.
```

The hook then:

1. validates the flow;
2. hashes the complete proposal;
3. writes verifier files under `.specflow/verifiers/<digest>/`;
4. resolves `{verifier_root}`;
5. writes `.specflow/SPEC.md` and the approved flow;
6. enters `red`.

### 3. Approved flow runs automatically

At `Stop` in `red`, the frozen verifier confirms the bug exists and advances automatically to `green`.

The model fixes `src/parser.py`. At the next `Stop`, the verifier advances to `review`. The final verifier runs the full tests and completes the workflow.

No human approval is required for normal transitions.

If the model discovers that the approved contract is wrong or incomplete, it calls:

```text
specflow.request_change({"reason": "...", "proposal": "..."})
```

Execution pauses. The human resumes planning with:

```text
%revise Add the missing invariant and update the verifier.
```

A revised proposal must again be approved before execution resumes.
