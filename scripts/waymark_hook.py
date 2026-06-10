#!/usr/bin/env python3
"""Optional Claude Code hook support for Waymark runs.

Hooks are deliberately observational. They never create facts or intents.

With WAYMARK_STRICT_HOOKS=1 the Stop hook additionally becomes a read-only
completion gate: it blocks stopping (exit code 2) while the active run's
`waymark final-status --json` is neither ready nor handed off. The gate only
reads the one completion authority; repairing state stays with the protocol
loop and the workers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STRICT_ENV = "WAYMARK_STRICT_HOOKS"

NEXT_ACTIONS = {
    "not_completed": (
        "continue the protocol loop (claim and conclude intents, then `waymark complete`)"
    ),
    "audit_failed": (
        "repair the completion graph through the protocol loop until `waymark audit --json` reports ok=true"
    ),
    "verification_missing": (
        "dispatch the verifier and persist its outcome with `waymark verification-record`"
    ),
    "verification_failed": (
        "address the failure, then re-run the verifier and persist a passing `waymark verification-record`"
    ),
}
DEFAULT_NEXT_ACTION = "inspect the run with `waymark final-status --run <run> --json` and continue the protocol loop"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def find_active_run(cwd: Path) -> Path | None:
    root = cwd / ".waymark"
    if not root.exists():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "blackboard.sqlite").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def is_waymark_cli_call(event: str, payload: dict) -> bool:
    """Waymark CLI invocations already log to events.jsonl through the CLI;
    mirroring them into hook-events.jsonl is pure double-logging."""
    if event != "post-tool-use":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    return isinstance(command, str) and "waymark" in command


def block_stop(run: Path, status_label: str, errors: list[str], next_action: str) -> int:
    """Exit code 2 makes Claude Code feed stderr back to the model, so the
    message must carry everything needed to act without re-deriving state."""
    lines = [
        "Waymark strict stop gate: the active run is not ready to stop.",
        f"run: {run}",
        f"final-status.status: {status_label}",
    ]
    if errors:
        lines.append("errors: " + "; ".join(errors))
    lines.append(f"next: {next_action}")
    lines.append(
        "Stopping is allowed once `waymark final-status --json` reports ready=true or the run reaches handoff."
    )
    print("\n".join(lines), file=sys.stderr)
    return 2


def strict_stop_gate(run: Path) -> int:
    """Read-only completion gate for Stop events.

    The gate never mutates graph state; it only consults the single completion
    authority. Missing or failed verification must be repaired by the protocol
    loop, not by the hook.
    """
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    command = [str(root / "bin" / "waymark"), "final-status", "--run", str(run), "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return block_stop(run, "unavailable (final-status could not run)", [str(exc)], DEFAULT_NEXT_ACTION)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return block_stop(
            run, f"unavailable (final-status exited {result.returncode})", [detail], DEFAULT_NEXT_ACTION
        )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        status = None
    if not isinstance(status, dict):
        return block_stop(
            run,
            "unavailable (final-status returned malformed JSON)",
            [result.stdout.strip()[:200]],
            DEFAULT_NEXT_ACTION,
        )
    if status.get("ready") is True:
        return 0
    if status.get("should_handoff") is True or status.get("status") == "handoff":
        return 0
    label = str(status.get("status") or "unknown")
    raw_errors = status.get("errors")
    errors = [str(item) for item in raw_errors if item] if isinstance(raw_errors, list) else []
    return block_stop(run, label, errors, NEXT_ACTIONS.get(label, DEFAULT_NEXT_ACTION))


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}

    if is_waymark_cli_call(event, payload):
        return 0

    cwd = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    run = find_active_run(cwd)
    if run is None:
        return 0

    hook_event = {
        "created_at": now(),
        "type": f"hook.{event}",
        "payload": payload,
    }
    events_path = run / "hook-events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(hook_event, sort_keys=True) + "\n")

    if event == "stop":
        print(
            "Waymark run detected. If graph state changed, print WAYMARK_GRAPH_CHECKPOINT "
            "after running `waymark checkpoint --json`."
        )
        # stop_hook_active means a Stop hook already blocked once this cycle;
        # gating again would loop the session instead of letting it act.
        if os.environ.get(STRICT_ENV) == "1" and not payload.get("stop_hook_active"):
            return strict_stop_gate(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
