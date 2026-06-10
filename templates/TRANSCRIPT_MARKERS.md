# Waymark Transcript Markers

Required transcript markers:

- `WAYMARK_RUN_START`
- `WAYMARK_GRAPH_CHECKPOINT`
- `WAYMARK_BOOTSTRAP_RESULT`
- `WAYMARK_REASON_VERIFY`
- `WAYMARK_EXPLORE_RESULT`
- `WAYMARK_VERIFICATION`
- `WAYMARK_COMPLETION_INTENT`
- `WAYMARK_FINAL_AUDIT`
- `WAYMARK_RUN_COMPLETE`
- `WAYMARK_HANDOFF`

`WAYMARK_RUN_COMPLETE` is valid only after `waymark audit --json` reports a completed project, a completion intent pointing at the goal fact, supporting source fact IDs, every acceptance criterion mapped to facts, a clean final audit, and `verifier-worker` confirmation (`WAYMARK_VERIFICATION` with `verified=true`) and no active handoff.
