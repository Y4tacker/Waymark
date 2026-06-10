# Waymark State

Run: `{run_dir}`
Project: `{project_id}`
Status: `{status}`
Bootstrap Enabled: `{bootstrap_enabled}`
Bootstrap Attempted: `{bootstrap_attempted}`
Bootstrap Result: `{bootstrap_result}`
Bootstrap Worker: `{bootstrap_worker}`
Rounds: `{round_count}` (without progress: `{rounds_without_progress}` of max `{max_rounds}`)

## Objective

See `OBJECTIVE.md` (origin, goal, and acceptance criteria).

## Latest Checkpoint

```json
{checkpoint_json}
```

## Next Loop

1. Run `waymark round-start --run "{run_dir}" --json`; hand off if it returns `should_handoff=true`.
2. Run `waymark checkpoint --run "{run_dir}" --json`.
3. Dispatch eligible workers through the CLI blackboard only (`should_reason` gates reason-worker).
4. Regenerate evidence with `waymark audit --run "{run_dir}" --json`; confirm with `verifier-worker` before completion.
5. Print transcript markers only when the evidence supports them.
