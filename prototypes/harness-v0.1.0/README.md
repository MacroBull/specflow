# SpecFlow Harness (`sf`) — executable prototype v0.1.0

> **Spec is the contract; DAG is the plan; node verifiers own state transitions.**
>
> Prototype, not a production security boundary. The legacy .NET SpecFlow name and several active agent projects already use this name; public branding/package names need separate review.

## What works today

- `sf propose`: create an editable Markdown spec, optionally with a native CLI planning agent.
- `sf lock --approve`: parse all requirement IDs and acceptance checks; compile a hand-authored/agent-generated graph; expand nested flows; reject cycles and unknown node types; stamp the exact spec SHA-256.
- `sf run`: execute atomic operations, commands, model-only output stages, or native CLI agent stages, with ordered dependencies, bounded node retries, event logs and persistent states.
- Re-run node invariants and final user acceptance **after all nodes**, regardless of what the agent claimed.
- `sf verify`, `sf inspect`, `sf graph`, `sf status`, `sf trace`, `sf retry`, `sf doctor`.
- Generate **optional** project hooks for Codex, Claude Code, and Copilot CLI, with stop feedback and startup context. Hook controls are advisory, not the source of truth.
- Standard library runtime, no web service or SQL server.

**Not implemented:** OS-enforced isolation for every provider, parallel scheduling, dynamic branching, remote workers, distributed locking, per-tool authorization, schema-guided prompts for all CLIs, tool permission equivalence, a large atomic catalog, multi-user concurrency, or a promise of task completion. Agent CLIs were not present in the test environment; adapters are based on published CLI interfaces but are not integration-tested here.

## Installation

Requires Python 3.11+:

```bash
cd specflow
python -m pip install -e .
sf --help
```

Or without installing:

```bash
PYTHONPATH=src python -m specflow --help
```

To run agent nodes, install and authenticate **at least one** supported tool in the host environment: `codex`, `claude`, or `copilot`. SpecFlow does **not** store model credentials.

## The minimal workflow

In a target project directory:

```bash
sf propose 'Add a CLI healthcheck that prints healthy' \
  --name add-healthcheck \
  --requirement 'CLI emits healthy and exits successfully' \
  --check '[R1] command: python -m mypackage healthcheck'

# Review and edit .specflow/specs/add-healthcheck.md!
sf lock add-healthcheck --approve
sf inspect add-healthcheck
sf graph add-healthcheck
sf run add-healthcheck --agent codex
sf verify add-healthcheck
sf status
```

If you want an LLM to draft the Markdown, use `sf propose ... --agent claude`. If you want an LLM to plan a multi-node DAG, use `sf lock ... --planner codex --approve`. **Review generated acceptance checks and plans before approval**. To supply a deterministic graph, use `sf lock ... --graph graph.json --approve`. Never let the planner declare its own task successful.

## CLI behavior

| Command | Role |
|---|---|
| `sf propose` | Produce human-readable, editable spec |
| `sf lock` | Validate spec coverage and compile nested graph into locked JSON |
| `sf run` | Create a run; execute nodes; verify all invariants and acceptance |
| `sf run CHANGE --run-id ID` | Resume an existing run with unchanged graph |
| `sf retry RUN_ID NODE` | Explicitly acknowledge a failed or interrupted node rerun |
| `sf verify CHANGE` | Independently rerun current acceptance plus per-node checks |
| `sf inspect CHANGE` | Explain requirement IDs, lock freshness and node dependencies |
| `sf graph CHANGE` | Print Mermaid diagram |
| `sf status` / `sf trace RUN_ID` | Run summary / append-only event journal |
| `sf doctor` | Local adapter discovery |
| `sf hook print --provider codex|claude|copilot` | Print optional native hook JSON; does not overwrite config |

Pass `--project PATH` **before** the subcommand to operate in a different workspace.

## Spec contract

Markdown is intentionally constrained in machine-interpreted sections. Natural-language descriptions are freeform; acceptance is executable:

```markdown
# Add healthcheck

## Goal
Expose a healthcheck through the package CLI.

## Requirements
- [R1] CLI returns a healthy status.
- [R2] Help text documents the command.

## Acceptance
- [R1] command: python -m mypackage healthcheck
- [R2] contains: README.md :: healthcheck

## Constraints
- Preserve all existing CLI commands.
```

Acceptance supports `file: RELPATH`, `contains: RELPATH :: literal`, and `command: argv ...`. Every requirement ID must have at least one acceptance check. `command` checks run executable arguments **without an intermediate shell**; project authors are responsible for keeping verification commands side-effect-free. `contains` is an exact literal match.

## DAG IR

Each node has `id`, `type`, optional `needs`, `checks`, `retries` and `timeout`. The current supported types:

| Type | Required fields | Typical use |
|---|---|---|
| `atomic` | `op`, `args` | Registered deterministic filesystem/JSON primitives |
| `command` | `argv` | Existing command / test runner / externally-developed tool |
| `agent` | `prompt`, optional `profile` | Workspace-changing bounded coding task |
| `model` | `prompt`, `output`, optional `format` | Generate a JSON/text artifact using reviewer profile by default |
| `verify` | `checks` | Pure validation gate (checks may run test commands) |
| `flow` | `file`, optional `with` | Compile-time nested subgraph (not a runtime node) |

Current atomic registry: `text.write@1`, `json.write@1`, `fs.copy@1`, `fs.mkdir@1`. Each implementation can be replaced without changing the surrounding DAG edges or its output checks. Future registry versions may add `test.run@1`, `git.patch@1`, etc., without overloading one generic `shell` operation.

`model` writes its response to the requested workspace path using the harness; it checks JSON syntax with `format: json`, not semantic correctness. Provider read-only modes are **not** universally tool-free. For strongly isolated model calls use an external sandbox or a future dedicated model adapter.

Example nested DAG in `examples/hello/graph.json`:

```json
{
  "nodes": [
    {"id": "compose", "type": "flow", "file": "flows/write.json", "with": {"message": "hello specflow"}},
    {"id": "summarize", "type": "command", "needs": ["compose"], "argv": ["python", "-c", "..."]}
  ]
}
```

Flow expansion is static: `compose` expands to `compose.write`, `compose.inspect`; dependencies on `compose` connect to terminal nodes of the expansion. Cycle checking and topological ordering run at lock time. The final `sf-final-acceptance` node is inserted automatically after all graph sinks. There is no hidden dynamic agent-driven graph mutation.

## Hooks

```bash
sf hook print --provider codex
sf hook print --provider claude
sf hook print --provider copilot
```

Merge the printed config into your project's native hook configuration, e.g. `.codex/hooks.json`, `.claude/settings.json`, or `.github/hooks/specflow.json`. Do **not** overwrite pre-existing hooks. Codex may require explicit trust approval for project hooks. Generated hooks activate only when `SPECFLOW_RUN_DIR`, `SPECFLOW_NODE_ID`, and `SPECFLOW_PROJECT` exist (set by the managed agent adapter).

- `SessionStart` reinjects active node identity as contextual guidance (including after supported session lifecycle events).
- `Stop` checks the node's declared checks and asks for one more repair turn on failure; it respects `stop_hook_active` to avoid recursion. Final success is determined **outside** the agent by `sf run`.
- This prototype does **not** include an enforcement-grade `PreToolUse` gate. Permissions and sandbox boundaries belong to the native runtime or a container. Do not confuse a prompt with a filesystem permission.

### Runtime-specific caveats

- **Codex:** uses `codex exec --sandbox workspace-write` for builder and `read-only` for planner/reviewer. Actual hook execution depends on trust and active config layers.
- **Claude Code:** uses `claude -p` with `plan` for planner/reviewer and `acceptEdits` for builder; tool permissions still depend on user configuration.
- **Copilot CLI:** uses `copilot -p ... -s`. Programmatic auto-approval (`--auto-approve`) passes `--allow-all-tools`; this is **not** an OS sandbox and can be risky. Its CLI/cloud support and prompt hook behavior are not identical.

Use independent/containerized agents for sensitive repos or untrusted inputs. A compromised project can influence agents through README/tool outputs; instruction precedence is not guaranteed by text alone.

## Persistence and failure

```text
.specflow/
  specs/<change>.md       editable natural-language contract
  locked/<change>.json    compiled DAG + spec content hash
  runs/<run-id>/
    state.json            node attempts, evidence, status, last error
    events.jsonl          event audit trail
```

Transitions: `PENDING -> RUNNING -> SUCCEEDED|FAILED`. A failed node blocks its dependents. `sf run` with the same ID will not silently rerun a node left in `RUNNING` by a crash or a node that exhausted retries. The operator must issue `sf retry RUN_ID NODE_ID`. `sf verify` reruns checks, even after a previously successful run. Global run outcomes in this prototype are `RUNNING`, `SUCCEEDED` and `BLOCKED` (where `BLOCKED` includes verification errors and unrecovered failures).

**Execution is at-least-once across deliberate retries**, not transactionally exactly-once. Make external side effects idempotent, attach idempotency keys, or use human confirmation. File evidence does not cover invisible external system state.

## Local deterministic demonstration

Run the example against a fresh workspace **without requiring any agent CLI**:

```bash
export PYTHONPATH=/path/to/specflow/src
mkdir -p /tmp/sf-demo
python -m specflow --project /tmp/sf-demo propose 'Create greeting and summary' \
  --name hello-demo \
  --requirement 'Greeting exists' \
  --requirement 'Summary exists' \
  --check '[R1] contains: output/hello.txt :: hello specflow' \
  --check '[R2] contains: output/summary.txt :: hello specflow'
python -m specflow --project /tmp/sf-demo lock hello-demo \
  --graph /path/to/specflow/examples/hello/graph.json --approve
python -m specflow --project /tmp/sf-demo run hello-demo
python -m specflow --project /tmp/sf-demo verify hello-demo
```

## Test

```bash
PYTHONPATH=src python -m pytest -q
```

The prototype is intended to be readable, forkable, and replaceable. Its strongest feature is an explicit success predicate, not any claim that agents are deterministic.
