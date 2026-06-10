# Waymark Autonomous Protocol

Run directory: `{run_dir}`
Project ID: `{project_id}`

## Invariant

SQLite `events`, `facts`, `intents`, `intent_sources`, `hints`, `criteria`, and `projects` are the source of truth. `STATE.md`, `graph.yaml`, `timeline.txt`, `events.jsonl`, and `reports/final.md` are generated or append-only views. Hook observations go to `hook-events.jsonl` and are never audit truth. Workers coordinate semantic state only through `waymark` CLI commands.

## Start Marker

Print:

```text
WAYMARK_RUN_START run={run_dir}
```

## Dispatch Loop

Repeat until completion or handoff:

1. Initialize or resume the run from this protocol file.
2. Open the round with `waymark round-start --run "{run_dir}" --json`. The CLI counts consecutive rounds without semantic progress. If it returns `should_handoff=true`, print `WAYMARK_HANDOFF` with the open and abandoned intents plus the latest checkpoint, then stop.
3. Read `STATE.md` and run `waymark checkpoint --run "{run_dir}" --json`.
4. If `checkpoint.status=active`, `checkpoint.bootstrap_enabled=true`, and `checkpoint.bootstrap_attempted=false`, dispatch `bootstrap-worker`. The bootstrap worker must either call `waymark bootstrap-complete --run "{run_dir}" --worker bootstrap-worker --stdin` or `waymark bootstrap-noop --run "{run_dir}" --worker bootstrap-worker --stdin`.
5. After `bootstrap-worker` returns, always read a fresh checkpoint. If `checkpoint.bootstrap_result=noop` and the project is still active, continue into the reason phase.
6. If `checkpoint.should_reason=true` and no reason lease exists, dispatch `reason-worker`. The CLI computes `should_reason` from genuinely new information (new facts, new hints, or open intents draining to zero) — do not dispatch `reason-worker` when it is false, and do not re-dispatch it merely because intents were created. The reason worker claims and releases its own reason lease, creates needed intents with the CLI, or completes the project with the CLI.
7. If `checkpoint.open_intent_count` is greater than zero, dispatch `explore-worker`. The explore worker claims one open intent itself, heartbeats as needed, and either concludes or releases that intent. The CLI claims intents oldest-first with priority override and abandons an intent after three failed releases; abandoned intents appear in `checkpoint.abandoned_intent_count` for the next reason pass to review.
8. After each worker batch, run `waymark checkpoint --run "{run_dir}" --json` to detect graph changes, then run `waymark snapshot --run "{run_dir}"` and print:

```text
WAYMARK_GRAPH_CHECKPOINT <checkpoint-json>
```

9. Run `waymark audit --run "{run_dir}" --json`.
10. If audit returns `ok=true`, dispatch `verifier-worker` before declaring completion. The verifier runs `waymark verify --run "{run_dir}" --json`, re-executes recorded `evidence_cmd` commands, checks `evidence_path` files, reports re-verified versus trust-prior coverage, and must persist its verdict — pass or fail — with `waymark verification-record --run "{run_dir}" --worker verifier-worker --stdin`. Print its result as:

```text
WAYMARK_VERIFICATION <verifier-json>
```

11. Run `waymark final-status --run "{run_dir}" --json`. This is the single completion authority: it combines the structural audit, the criteria check, and the latest durable verification record. If and only if it returns `ready=true`, print `WAYMARK_COMPLETION_INTENT`, `WAYMARK_FINAL_AUDIT`, and finally:

```text
WAYMARK_RUN_COMPLETE project_status=completed
```

12. If `final-status` returns `status=verification_failed` or `status=verification_missing` after the verifier ran, print `WAYMARK_HANDOFF` with the verifier's reason and the latest checkpoint. Do not loop reopen attempts autonomously.

## Prohibitions

- The main protocol must not claim explore intents before dispatching `explore-worker`.
- Workers must not return JSON proposals for the main protocol to persist.
- Completion must not be declared only in text. It must be represented by a concluded completion intent pointing to `goal`.
- `reason-worker` is the only worker that creates new exploratory intents.
- `explore-worker` is the only worker that claims or concludes open exploratory intents.
- `bootstrap-worker` only handles direct completion and must not create partial exploratory facts.
- The main protocol must use `checkpoint.bootstrap_attempted`; transcript markers must not be used to decide whether bootstrap already ran.
- The main protocol must use `round-start` and `checkpoint.should_handoff` for stall decisions; it must not improvise its own progress heuristic.
- Verifier transcript output is not completion evidence by itself. The verifier must persist `waymark verification-record`, and the protocol must gate completion on `waymark final-status` returning `ready=true`.

## Completion Condition

Completion requires:

- `status=completed`.
- A completion intent whose `to_fact_id` is the special `goal` fact.
- Non-goal source fact IDs supporting completion.
- Every acceptance criterion (if any were defined) mapped to supporting facts.
- `waymark audit --json` returns `ok: true`.
- `verifier-worker` confirms the evidence (`WAYMARK_VERIFICATION` with `verified=true`) and persists it with `waymark verification-record`.
- `waymark final-status --json` returns `ready=true`.
- No `WAYMARK_HANDOFF` marker appears after the final audit.

`final-status` is the single completion authority: it combines the structural audit, the criteria check, and the latest durable verification record. `doctor` aggregates environment checks and the completion audit; for active projects it reports `status=not_completed_yet`. `/goal` completion must be based only on `final-status.ready=true`, not on `doctor.ok` or transcript claims.
