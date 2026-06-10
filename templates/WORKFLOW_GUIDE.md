# Waymark Dynamic Workflow Runtime (Optional Scale Mode)

This guide defines the adapter contract for driving a Waymark run with Claude Code Dynamic Workflow instead of the default `/goal + PROTOCOL.md` supervisor. Use it when a run needs parallel explore waves or is too large for one supervisor transcript. For normal use, prefer `/goal` — it is simpler to debug and the default runtime.

Dynamic Workflow does not replace the blackboard. It is only an optional dispatcher. SQLite remains the source of truth in both modes.

A near-runnable Dynamic Workflow reference dispatcher for this contract lives in `templates/DYNAMIC_WORKFLOW_REFERENCE.js`. It is illustrative, not authoritative: Claude already knows how to run workflows, and the file only documents Waymark's scheduling rules. It is not the default runtime, not installed as a native runtime, and not required for normal `/goal` use. To try it, initialize the run first with `waymark init` (or the `waymark-goal` skill), then launch it with `{ run: "<run-dir>" }` as workflow args from a session with the Waymark plugin loaded. The reference consumes only structured JSON from CLI read commands and dispatches the four worker agent types; it contains no state mutation of its own. Its `MAX_PARALLEL_EXPLORE = 4` cap is a safe starting point, not an architectural limit — a real workflow could read it from args (`args.maxParallelExplore || 4`).

## Adapter Contract

- The workflow script dispatches workers; it owns scheduling, not state.
- Workers call the `waymark` CLI themselves to mutate state (claims, conclusions, completions). The script must not mutate the blackboard directly except by asking workers to run CLI commands.
- The script must not treat agent text output as truth. Decisions read `waymark checkpoint`, `waymark audit`, `waymark verify`, `waymark final-status`, or the generated views — never a worker's prose claim.
- Parallel explore waves are safe by construction: each explore worker claims its own intent through `waymark intent-claim`, and the CLI's `BEGIN IMMEDIATE` transactions prevent double-claims.
- The verifier must persist its verdict with `waymark verification-record`; the script gates completion on `waymark final-status` returning `ready=true` — the same single authority the `/goal` protocol uses.

## Recommended Scale Loop

```text
round-start
checkpoint
bootstrap if needed
reason if should_reason
spawn up to N explore workers if open_intent_count > 0
audit
verifier
final-status
```

Keep the parallel wave conservative:

```text
max_parallel_explore = min(open_intent_count, 4)
```

Each explore worker in a wave should use a distinct `--worker` name (for example `explore-1`, `explore-2`) so leases and strikes attribute correctly.

## Round Semantics For Parallel Runs

`round-start` measures semantic progress between rounds. Call it only after the previous worker batch has fully finished: one workflow round equals one completed agent wave. Calling `round-start` while workers are still running splits a wave's progress across two rounds and can fake a stall (premature `should_handoff`) or hide one.

## Stop Conditions

- `final-status.ready=true` — the run is complete and verified.
- `round-start` returns `should_handoff=true` — the run stalled; surface open and abandoned intents to a human.
- `final-status.status=verification_failed` — completion was disproven; do not loop reopen attempts autonomously.
