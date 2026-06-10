---
name: waymark-goal
description: "Initialize a Waymark CLI blackboard run and print a ready-to-paste /goal command for durable autonomous execution. Drafts a mission contract (origin, goal, falsifiable acceptance criteria, constraints, assumptions, handoff triggers), clarifies only true semantic gaps, and holds the contract for user review before waymark init. Use when the user asks to run a task through Waymark, start a Waymark goal or run, or create a durable Fact-Intent blackboard run for any long-horizon task — software, research, writing, data analysis, or planning. Trigger keywords: waymark, blackboard, /goal, durable run, fact-intent, mission contract, autonomous execution."
---

# Waymark Goal

Waymark is domain-general: it runs software work, research, writing, data analysis, planning, and other long-horizon tasks. This skill does not plan the work — planning happens inside the run, by reason workers, as evidence accumulates. The job here is to seed the blackboard with a correct **mission contract**, because `waymark init` is the semantic commitment point: it persists the goal and criteria into SQLite, and the run's completion is gated on exactly those criteria.

If the task is very small (roughly under an hour, a single deliverable), say it does not need Waymark and suggest just doing it directly. Do not force the machinery.

## Procedure

1. Choose a concise task slug from the user request, and an id: the next free zero-padded sequence number among existing `.waymark/<task-slug>-*` directories (`-001`, `-002`, …).
2. Draft the mission contract — do not initialize yet:
   - a short title,
   - `origin` — the starting context,
   - `goal` — the desired final outcome, an end-state rather than a task body,
   - `acceptance criteria` — 2–5 falsifiable checks. Each criterion must be observable and evidence-oriented — "tests pass via python3 -m unittest" or "the brief at reports/final.md cites at least five primary sources" rather than "it works" or "it reads well",
   - `constraints` — boundaries the run must respect: scope edges, untouchable areas, budgets, binding deadlines,
   - `assumptions` — anything material you inferred instead of asking, listed so the review gate can correct them,
   - `handoff triggers` — semantic conditions, beyond the built-in no-progress budget, under which autonomous work should stop and return to the human,
   - runtime limits if they should differ from defaults (`--max-rounds`, `--max-intents`).

   Draft only the fields the task makes material; leave the rest out rather than padding. Do not walk a domain checklist: stack, tooling, format, or interface details are assumptions to surface at review, not questions to ask, unless the user's domain makes them material.
3. Clarify true semantic gaps only. Ask before the review gate only when a missing answer would materially change the mission contract. True semantic gaps are:
   - the desired end state,
   - scope boundaries,
   - acceptance evidence — what observable proof counts,
   - constraints,
   - priority tradeoffs,
   - risk tolerance,
   - handoff or stop conditions.

   Do not ask about implementation details that can be safely inferred; record those as assumptions and let the review gate correct them. Zero questions on a well-specified request is the expected case. Use `AskUserQuestion` for any clarification asked before the review gate resolves, instead of asking only in plain text. If `AskUserQuestion` is unavailable in the current host, ask in plain text and state that the tool is unavailable.
4. Review gate. Present the mission contract compactly (goal, origin, criteria as a numbered list, constraints, assumptions, handoff triggers, and limits), then ask the user to confirm with at most four options:
   - **Start now** (first option) — proceed to init.
   - **Adjust the goal or outcome.**
   - **Adjust the criteria or evidence.**
   - **Adjust scope, constraints, or handoff triggers.**

   Apply the chosen revision, re-present the contract, and loop until the user picks Start now. Wait for the answer: never run `waymark init` on silence, and never assume confirmation. The blackboard must not be seeded with unconfirmed semantics.

   Skip the gate only when there is nothing to review: the user explicitly asked to proceed without confirmation, or the request already provides an unambiguous goal and explicit, falsifiable acceptance criteria. When skipping, say so in one line ("Locking the mission contract as specified — criteria were explicit in the request.") and state any assumptions inline.
5. After confirmation, create the project-local run directory `.waymark/<task-slug>-<id>/` and run:

```bash
waymark init --run ".waymark/<task-slug>-<id>" --title "<short title>" --origin "<starting context>" --goal "<desired final outcome>" \
  --criterion "<falsifiable check 1>" --criterion "<falsifiable check 2>"
```

`init` persists `origin`, `goal`, and each `--criterion` directly — completion is gated on exactly these. Carry the rest of the confirmed contract as repeated `--hint` flags ("constraint: …", "assumption: …", "handoff: …"); hints guide workers but are not graph evidence. `--max-rounds <n>` (default 3) bounds consecutive no-progress supervisor rounds before handoff; raise it only for long-horizon tasks. Init records the current git HEAD as the run's baseline ref automatically when inside a repository.

If `waymark` is not on PATH, run `bin/waymark` from the plugin root (`${CLAUDE_PLUGIN_ROOT}/bin/waymark`). If init fails with "blackboard already exists", choose the next free id instead; `--force` wipes that run's history and is only for an explicitly requested restart of the same run.

6. Verify init generated these files (it creates all of them; never write them by hand):

- `blackboard.sqlite`
- `OBJECTIVE.md`
- `PROTOCOL.md`
- `STATE.md`
- `graph.yaml`
- `timeline.txt`
- `events.jsonl`
- `reports/final.md`

7. If the task concerns a repository, run only lightweight recon such as `git status --short`, `rg --files | sed -n '1,80p'`, and obvious README/package inspection. Store recon as a hint with:

```bash
printf '%s\n' '{"content":"..."}' | waymark hint-add --run ".waymark/<task-slug>-<id>" --creator "waymark-goal" --stdin
```

8. Print exactly one ready-to-paste `/goal` line:

```text
/goal "Run the Waymark protocol in .waymark/<task-slug>-<id>/PROTOCOL.md until WAYMARK_RUN_COMPLETE appears with project status=completed, completion intent ID, goal fact ID, supporting fact IDs, all acceptance criteria mapped, final audit clean, WAYMARK_VERIFICATION verified=true, and no WAYMARK_HANDOFF."
```

## Rules

- Two semantic human gates only: clarification for true gaps, then mission contract review before init.
- The final `/goal` paste is a required slash-command dispatch step, not an extra planning gate: slash commands fire only from user input, so the skill cannot invoke `/goal` itself — tell the user to paste the printed line. Everything after the pasted `/goal` runs autonomously through `PROTOCOL.md` until `final-status.ready=true` or handoff.
- Never run `waymark init` before the review gate resolves.
- Do not write mutable run state to `${CLAUDE_PLUGIN_ROOT}` — it is the shared read-only plugin install; runs live project-local under `.waymark/`.
- Use `${CLAUDE_PLUGIN_DATA}` only for global cache or dependencies.
- Recommend adding `.waymark/` to `.gitignore` unless the user wants to commit run records.
- All semantic graph writes must go through `waymark` CLI commands — hand-editing `blackboard.sqlite` or the generated projections bypasses the event log and breaks audit integrity.
