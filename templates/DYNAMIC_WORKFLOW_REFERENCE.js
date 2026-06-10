// Waymark Dynamic Workflow reference dispatcher (optional scale path).
//
// This file is a near-runnable REFERENCE, not an installed runtime. Claude
// already knows how to run Dynamic Workflows; this file only documents the
// Waymark-specific scheduling rules — what to dispatch, in what order, and
// which CLI reads gate each decision. It is not the default runtime, it is
// not installed as a native workflow, and it is not required for normal
// `/goal + PROTOCOL.md` use.
//
// To try it, invoke from a session that has the Waymark plugin loaded (so the
// worker agent types resolve), after `waymark init` has created the run:
//
//   Workflow({ scriptPath: "templates/DYNAMIC_WORKFLOW_REFERENCE.js", args: { run: ".waymark/<slug>-<id>" } })
//
// Design boundary (see templates/WORKFLOW_GUIDE.md): Dynamic Workflow may
// schedule agents, but it must not own state.
// - This script is a dispatcher only. It never mutates SQLite directly and
//   never trusts worker prose. Every durable state change happens inside a
//   worker through `bin/waymark`; every decision below reads structured JSON
//   from a CLI read command executed by a read-only agent.
// - SQLite plus the CLI is the authority. The workflow is just a scheduler.
// - Completion happens only when `waymark final-status --json` returns ready=true.

export const meta = {
  name: 'waymark-scale-run',
  description: 'Reference dispatcher: drive a Waymark blackboard run with bounded parallel explore waves',
  phases: [
    { title: 'Round', detail: 'round-start + checkpoint reads' },
    { title: 'Bootstrap', detail: 'one direct-completion attempt' },
    { title: 'Reason', detail: 'plan intents when should_reason' },
    { title: 'Explore', detail: 'bounded parallel explore wave' },
    { title: 'Verify', detail: 'audit, verifier, final-status gate' },
  ],
}

const RUN = args && args.run
if (!RUN) throw new Error('pass {run: "<run-dir>"} as workflow args (create it first with `waymark init`)')

// Conservative reference default for one explore wave — a safe starting point,
// not an architectural limit. Raise it deliberately, not by default; a real
// workflow could accept it from args instead: `args.maxParallelExplore || 4`.
const MAX_PARALLEL_EXPLORE = 4

// Minimal schemas for the CLI JSON this script consumes. additionalProperties
// stays true so new CLI fields never break the reference dispatcher.
const ROUND_SCHEMA = {
  type: 'object',
  required: ['round_count', 'rounds_without_progress', 'should_handoff'],
  properties: {
    round_count: { type: 'integer' },
    rounds_without_progress: { type: 'integer' },
    max_rounds: { type: 'integer' },
    should_handoff: { type: 'boolean' },
  },
  additionalProperties: true,
}

const CHECKPOINT_SCHEMA = {
  type: 'object',
  required: ['status', 'bootstrap_enabled', 'bootstrap_attempted', 'should_reason', 'open_intent_count'],
  properties: {
    status: { type: 'string' },
    bootstrap_enabled: { type: 'boolean' },
    bootstrap_attempted: { type: 'boolean' },
    should_reason: { type: 'boolean' },
    open_intent_count: { type: 'integer' },
    abandoned_intent_count: { type: 'integer' },
    reason_worker: { type: ['string', 'null'] },
  },
  additionalProperties: true,
}

const AUDIT_SCHEMA = {
  type: 'object',
  required: ['ok'],
  properties: { ok: { type: 'boolean' }, errors: { type: 'array', items: { type: 'string' } } },
  additionalProperties: true,
}

const FINAL_STATUS_SCHEMA = {
  type: 'object',
  required: ['ready', 'status'],
  properties: {
    ready: { type: 'boolean' },
    status: { type: 'string' },
    audit_ok: { type: 'boolean' },
    verification_ok: { type: 'boolean' },
    criteria_ok: { type: 'boolean' },
    errors: { type: 'array', items: { type: 'string' } },
  },
  additionalProperties: true,
}

// Read (or bookkeeping) CLI command via a small agent. The agent runs exactly
// one command and returns its JSON stdout as structured output — worker text is
// never the decision input.
const cli = (command, schema, phase) =>
  agent(
    `Run exactly this command from the project root and return its parsed JSON stdout as your structured output. ` +
      `Run no other command and add no commentary:\n\nbin/waymark ${command} --run "${RUN}" --json`,
    { label: `cli:${command}`, phase, schema },
  )

// Dispatch a Waymark worker. Workers are self-writing: they mutate the
// blackboard themselves through `bin/waymark` per their agent contracts.
const worker = (type, prompt, phase, label) => agent(prompt, { agentType: type, phase, label: label || type })

let final = null
while (true) {
  // One workflow round = one completed agent wave: round-start runs only after
  // the previous wave has fully finished (parallel() below is a barrier).
  const round = await cli('round-start', ROUND_SCHEMA, 'Round')
  if (round.should_handoff) {
    log(`stalled: ${round.rounds_without_progress}/${round.max_rounds} rounds without progress — handing off`)
    final = { ready: false, status: 'handoff', round }
    break
  }

  let cp = await cli('checkpoint', CHECKPOINT_SCHEMA, 'Round')

  if (cp.status === 'active' && cp.bootstrap_enabled && !cp.bootstrap_attempted) {
    await worker(
      'bootstrap-worker',
      `Waymark run directory: ${RUN}. Follow your contract: attempt direct verifiable completion via ` +
        `\`waymark bootstrap-complete\`, otherwise persist \`waymark bootstrap-noop\`.`,
      'Bootstrap',
    )
    cp = await cli('checkpoint', CHECKPOINT_SCHEMA, 'Bootstrap')
  }

  if (cp.status === 'active' && cp.should_reason && !cp.reason_worker) {
    await worker(
      'reason-worker',
      `Waymark run directory: ${RUN}. Follow your contract: claim the reason lease, read \`waymark brief\`, ` +
        `review struggling and abandoned intents, then create intents or complete with criteria mapping.`,
      'Reason',
    )
    cp = await cli('checkpoint', CHECKPOINT_SCHEMA, 'Reason')
  }

  if (cp.status === 'active' && cp.open_intent_count > 0) {
    const wave = Math.min(cp.open_intent_count, MAX_PARALLEL_EXPLORE)
    log(`explore wave: ${wave} workers for ${cp.open_intent_count} open intents`)
    // Safe by construction: each worker claims its own intent through
    // `waymark intent-claim`; BEGIN IMMEDIATE prevents double-claims. Distinct
    // --worker names keep leases and strikes attributable.
    await parallel(
      Array.from({ length: wave }, (_, index) => () =>
        worker(
          'explore-worker',
          `Waymark run directory: ${RUN}. Follow your contract using worker name "explore-${index + 1}": ` +
            `claim exactly one open intent with \`waymark intent-claim --worker explore-${index + 1}\`, read its ` +
            `\`waymark context\`, then conclude (PARTIAL: if out of turns) or release with a reason.`,
          'Explore',
          `explore-${index + 1}`,
        ),
      ),
    )
  }

  const audit = await cli('audit', AUDIT_SCHEMA, 'Verify')
  if (audit.ok) {
    await worker(
      'verifier-worker',
      `Waymark run directory: ${RUN}. Follow your contract: run \`waymark verify\`, re-execute evidence commands, ` +
        `check evidence paths and the baseline ref, then persist your verdict (pass or fail) with ` +
        `\`waymark verification-record\`.`,
      'Verify',
    )
  }

  // The single completion authority. Worker claims, verifier prose, and audit
  // alone never decide the outcome.
  final = await cli('final-status', FINAL_STATUS_SCHEMA, 'Verify')
  if (final.ready) {
    log('final-status ready=true — run complete and verified')
    break
  }
  if (final.status !== 'not_completed') {
    // ready/handoff handled above; audit_failed, verification_missing, and
    // verification_failed all stop the loop — do not retry reopen or
    // re-verification autonomously. Surface the decision object instead.
    log(`stopping on final-status=${final.status}`)
    break
  }
  // status === not_completed: project is active and unstalled — next round.
}

return final
