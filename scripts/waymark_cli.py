#!/usr/bin/env python3
"""SQLite-backed Waymark blackboard CLI.

The CLI is intentionally self-contained and stdlib-only so a Claude Code plugin
can ship it without external runtime setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EXIT_SUCCESS = 0
EXIT_NOOP = 1
EXIT_BAD_INPUT = 2
EXIT_LEASE_CONFLICT = 3
EXIT_INACTIVE = 4
EXIT_STORAGE = 5

# The CLI manages exactly one project per run directory. The schema keeps
# project_id columns so existing blackboards stay readable, but multi-project
# support is deliberately out of scope.
PROJECT_ID = "p001"
SPECIAL_FACTS = {"origin", "goal"}
COMPLETION_CREATOR = "completion"
DEFAULT_INTENT_TIMEOUT = 300
DEFAULT_REASON_TIMEOUT = 300
DEFAULT_MAX_INTENTS = 6
DEFAULT_MAX_ROUNDS = 3
MAX_RELEASES_BEFORE_ABANDON = 3
MAX_FACT_DESCRIPTION_CHARS = 1200
DEFAULT_BRIEF_FACTS = 10

# Events that track lease or bookkeeping churn, not graph semantics. The
# semantic event sequence (checkpoint.last_event_seq, round progress) ignores
# them so heartbeats and claims never count as progress.
NON_SEMANTIC_EVENT_TYPES = (
    "intent_claimed",
    "intent_heartbeat",
    "intent_released",
    "intent_lease_expired",
    "reason_claimed",
    "reason_heartbeat",
    "reason_released",
    "reason_lease_expired",
    "round_started",
    "snapshot",
)


class WaymarkError(Exception):
    exit_code = EXIT_BAD_INPUT


class NoopError(WaymarkError):
    exit_code = EXIT_NOOP


class LeaseConflictError(WaymarkError):
    exit_code = EXIT_LEASE_CONFLICT


class InactiveProjectError(WaymarkError):
    exit_code = EXIT_INACTIVE


class StorageError(WaymarkError):
    exit_code = EXIT_STORAGE


@dataclass
class Context:
    run: Path
    db_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def seconds_old(value: str | None) -> float:
    parsed = parse_time(value)
    if parsed is None:
        return 0
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def read_json_stdin(required: bool = True) -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        if required:
            raise WaymarkError("expected JSON on stdin")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WaymarkError(f"invalid JSON on stdin: {exc}") from exc
    if not isinstance(data, dict):
        raise WaymarkError("stdin JSON must be an object")
    return data


def normalize_worker_result(data: dict[str, Any]) -> dict[str, Any]:
    """Accept Waymark's wrapped shape and safe direct legacy shapes."""
    if "accepted" in data:
        if data.get("accepted") is not True:
            raise WaymarkError("worker result was not accepted")
        inner = data.get("data", {})
        if not isinstance(inner, dict):
            raise WaymarkError("worker result data must be an object")
        return inner
    return data


def ensure_run_dir(run: Path) -> Context:
    run.mkdir(parents=True, exist_ok=True)
    (run / "reports").mkdir(exist_ok=True)
    (run / "snapshots").mkdir(exist_ok=True)
    return Context(run=run, db_path=run / "blackboard.sqlite")


def require_context(run: Path) -> Context:
    ctx = Context(run=run, db_path=run / "blackboard.sqlite")
    if not ctx.db_path.exists():
        raise StorageError(f"blackboard does not exist: {ctx.db_path}")
    return ctx


@contextmanager
def connect(ctx: Context) -> Iterable[sqlite3.Connection]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(ctx.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 3000")
        ensure_schema_columns(conn)
        yield conn
    except sqlite3.Error as exc:
        raise StorageError(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterable[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
            bootstrap_attempted_at TEXT,
            bootstrap_worker TEXT,
            bootstrap_result TEXT,
            created_at TEXT NOT NULL,
            reason_worker TEXT,
            reason_trigger TEXT,
            reason_started_at TEXT,
            reason_last_heartbeat_at TEXT,
            baseline_ref TEXT,
            round_count INTEGER NOT NULL DEFAULT 0,
            rounds_without_progress INTEGER NOT NULL DEFAULT 0,
            last_round_seq INTEGER,
            last_reason_fact_count INTEGER,
            last_reason_hint_count INTEGER,
            last_reason_open_intent_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS facts (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            evidence_cmd TEXT,
            evidence_path TEXT,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS intents (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            to_fact_id TEXT,
            description TEXT NOT NULL,
            creator TEXT NOT NULL,
            worker TEXT,
            last_heartbeat_at TEXT,
            created_at TEXT NOT NULL,
            concluded_at TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            release_count INTEGER NOT NULL DEFAULT 0,
            last_release_reason TEXT,
            abandoned_at TEXT,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (to_fact_id, project_id) REFERENCES facts(id, project_id)
        );

        CREATE TABLE IF NOT EXISTS intent_sources (
            intent_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (intent_id, project_id, position),
            FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id)
        );

        CREATE TABLE IF NOT EXISTS hints (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            content TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS criteria (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS criterion_facts (
            criterion_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (criterion_id, project_id, position),
            FOREIGN KEY (criterion_id, project_id) REFERENCES criteria(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id)
        );

        CREATE TABLE IF NOT EXISTS verification_runs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            worker TEXT NOT NULL,
            verified INTEGER NOT NULL,
            evidence TEXT,
            reason TEXT,
            re_verified INTEGER,
            trust_prior INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            intent_timeout INTEGER NOT NULL,
            reason_timeout INTEGER NOT NULL,
            max_intents INTEGER NOT NULL,
            max_rounds INTEGER NOT NULL DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS scoped_counters (
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            value INTEGER NOT NULL,
            PRIMARY KEY (project_id, kind),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_intents_open ON intents(project_id, to_fact_id);
        CREATE INDEX IF NOT EXISTS idx_intents_worker ON intents(project_id, worker);
        CREATE INDEX IF NOT EXISTS idx_events_project_seq ON events(project_id, seq);
        """
    )
    ensure_schema_columns(conn)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_column_migrations(conn: sqlite3.Connection, table: str, migrations: dict[str, str]) -> None:
    existing = table_columns(conn, table)
    for column, statement in migrations.items():
        if column not in existing:
            conn.execute(statement)


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
    ).fetchone()
    if table_exists is None:
        return
    apply_column_migrations(
        conn,
        "projects",
        {
            "bootstrap_attempted_at": "ALTER TABLE projects ADD COLUMN bootstrap_attempted_at TEXT",
            "bootstrap_worker": "ALTER TABLE projects ADD COLUMN bootstrap_worker TEXT",
            "bootstrap_result": "ALTER TABLE projects ADD COLUMN bootstrap_result TEXT",
            "baseline_ref": "ALTER TABLE projects ADD COLUMN baseline_ref TEXT",
            "round_count": "ALTER TABLE projects ADD COLUMN round_count INTEGER NOT NULL DEFAULT 0",
            "rounds_without_progress": "ALTER TABLE projects ADD COLUMN rounds_without_progress INTEGER NOT NULL DEFAULT 0",
            "last_round_seq": "ALTER TABLE projects ADD COLUMN last_round_seq INTEGER",
            "last_reason_fact_count": "ALTER TABLE projects ADD COLUMN last_reason_fact_count INTEGER",
            "last_reason_hint_count": "ALTER TABLE projects ADD COLUMN last_reason_hint_count INTEGER",
            "last_reason_open_intent_count": "ALTER TABLE projects ADD COLUMN last_reason_open_intent_count INTEGER",
        },
    )
    apply_column_migrations(
        conn,
        "facts",
        {
            "evidence_cmd": "ALTER TABLE facts ADD COLUMN evidence_cmd TEXT",
            "evidence_path": "ALTER TABLE facts ADD COLUMN evidence_path TEXT",
        },
    )
    apply_column_migrations(
        conn,
        "intents",
        {
            "priority": "ALTER TABLE intents ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
            "attempt_count": "ALTER TABLE intents ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            "release_count": "ALTER TABLE intents ADD COLUMN release_count INTEGER NOT NULL DEFAULT 0",
            "last_release_reason": "ALTER TABLE intents ADD COLUMN last_release_reason TEXT",
            "abandoned_at": "ALTER TABLE intents ADD COLUMN abandoned_at TEXT",
        },
    )
    # Older blackboards used a settings table without a primary key; rebuild it
    # as the single-row shape. SQLite cannot ALTER a column into a PK.
    settings_columns = table_columns(conn, "settings")
    if settings_columns and "id" not in settings_columns:
        row = conn.execute("SELECT * FROM settings LIMIT 1").fetchone()
        conn.execute("DROP TABLE settings")
        conn.execute(
            """
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                intent_timeout INTEGER NOT NULL,
                reason_timeout INTEGER NOT NULL,
                max_intents INTEGER NOT NULL,
                max_rounds INTEGER NOT NULL DEFAULT 3
            )
            """
        )
        if row is not None:
            conn.execute(
                "INSERT INTO settings(id, intent_timeout, reason_timeout, max_intents, max_rounds) VALUES (1, ?, ?, ?, ?)",
                (row["intent_timeout"], row["reason_timeout"], row["max_intents"], DEFAULT_MAX_ROUNDS),
            )
    elif settings_columns and "max_rounds" not in settings_columns:
        conn.execute(
            f"ALTER TABLE settings ADD COLUMN max_rounds INTEGER NOT NULL DEFAULT {DEFAULT_MAX_ROUNDS}"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS criteria (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS criterion_facts (
            criterion_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (criterion_id, project_id, position),
            FOREIGN KEY (criterion_id, project_id) REFERENCES criteria(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_runs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            worker TEXT NOT NULL,
            verified INTEGER NOT NULL,
            evidence TEXT,
            reason TEXT,
            re_verified INTEGER,
            trust_prior INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("DROP TABLE IF EXISTS counters")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intents_open ON intents(project_id, to_fact_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intents_worker ON intents(project_id, worker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_project_seq ON events(project_id, seq)")


def settings(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO settings(id, intent_timeout, reason_timeout, max_intents, max_rounds) VALUES (1, ?, ?, ?, ?)",
            (DEFAULT_INTENT_TIMEOUT, DEFAULT_REASON_TIMEOUT, DEFAULT_MAX_INTENTS, DEFAULT_MAX_ROUNDS),
        )
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return row


def append_event(conn: sqlite3.Connection, ctx: Context, event_type: str, payload: dict[str, Any], project_id: str | None = PROJECT_ID) -> None:
    created_at = utc_now()
    payload_json = json.dumps(payload, sort_keys=True)
    conn.execute(
        "INSERT INTO events(project_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (project_id, event_type, payload_json, created_at),
    )
    ctx.run.mkdir(parents=True, exist_ok=True)
    with (ctx.run / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "created_at": created_at,
                    "project_id": project_id,
                    "type": event_type,
                    "payload": payload,
                },
                sort_keys=True,
            )
            + "\n"
        )


def require_project(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (PROJECT_ID,)).fetchone()
    if row is None:
        raise StorageError("project p001 not found")
    return row


def require_active(conn: sqlite3.Connection) -> None:
    project = require_project(conn)
    if project["status"] != "active":
        raise InactiveProjectError(f"project is {project['status']}")


def release_intent_lease(
    conn: sqlite3.Connection,
    ctx: Context,
    intent_id: str,
    release_reason: str,
) -> dict[str, Any]:
    """Clear an intent lease, count the failed attempt, and abandon repeat offenders.

    Every cleared lease (explicit release or lazy expiry) is a strike. After
    MAX_RELEASES_BEFORE_ABANDON strikes the intent is abandoned: it leaves the
    claimable pool and is surfaced through the checkpoint for reason review.
    """
    row = conn.execute(
        "SELECT release_count FROM intents WHERE project_id = ? AND id = ?",
        (PROJECT_ID, intent_id),
    ).fetchone()
    release_count = (row["release_count"] if row else 0) + 1
    abandoned = release_count >= MAX_RELEASES_BEFORE_ABANDON
    conn.execute(
        """
        UPDATE intents
        SET worker = NULL,
            last_heartbeat_at = NULL,
            release_count = ?,
            last_release_reason = ?,
            abandoned_at = CASE WHEN ? THEN ? ELSE abandoned_at END
        WHERE project_id = ? AND id = ?
        """,
        (release_count, release_reason, 1 if abandoned else 0, utc_now(), PROJECT_ID, intent_id),
    )
    if abandoned:
        append_event(
            conn,
            ctx,
            "intent_abandoned",
            {"intent_id": intent_id, "release_count": release_count, "reason": release_reason},
        )
    return {"release_count": release_count, "abandoned": abandoned}


def clear_expired_leases(conn: sqlite3.Connection, ctx: Context) -> None:
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (PROJECT_ID,)).fetchone()
    if project is None:
        return
    cfg = settings(conn)
    expired_intents = conn.execute(
        """
        SELECT id, worker, last_heartbeat_at FROM intents
        WHERE project_id = ? AND to_fact_id IS NULL AND abandoned_at IS NULL AND worker IS NOT NULL
        """,
        (PROJECT_ID,),
    ).fetchall()
    for intent in expired_intents:
        if seconds_old(intent["last_heartbeat_at"]) > cfg["intent_timeout"]:
            outcome = release_intent_lease(conn, ctx, intent["id"], "lease-expired")
            append_event(
                conn,
                ctx,
                "intent_lease_expired",
                {
                    "intent_id": intent["id"],
                    "worker": intent["worker"],
                    "release_count": outcome["release_count"],
                    "abandoned": outcome["abandoned"],
                },
            )

    if project["reason_worker"] and seconds_old(project["reason_last_heartbeat_at"] or project["reason_started_at"]) > cfg["reason_timeout"]:
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = NULL, reason_trigger = NULL, reason_started_at = NULL, reason_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (PROJECT_ID,),
        )
        append_event(
            conn,
            ctx,
            "reason_lease_expired",
            {"worker": project["reason_worker"], "trigger": project["reason_trigger"]},
        )


def next_scoped_id(conn: sqlite3.Connection, kind: str, prefix: str) -> str:
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (PROJECT_ID, kind),
    ).fetchone()
    value = 1 if row is None else row["value"] + 1
    conn.execute(
        """
        INSERT INTO scoped_counters(project_id, kind, value) VALUES (?, ?, ?)
        ON CONFLICT(project_id, kind) DO UPDATE SET value = excluded.value
        """,
        (PROJECT_ID, kind, value),
    )
    return f"{prefix}{value:03d}"


def fact_exists(conn: sqlite3.Connection, fact_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM facts WHERE project_id = ? AND id = ?",
            (PROJECT_ID, fact_id),
        ).fetchone()
        is not None
    )


def validate_sources(conn: sqlite3.Connection, source_ids: list[str], *, allow_goal: bool = False) -> None:
    if not source_ids:
        raise WaymarkError("at least one source fact is required")
    seen: set[str] = set()
    for fact_id in source_ids:
        if not isinstance(fact_id, str) or not fact_id:
            raise WaymarkError("source fact IDs must be non-empty strings")
        if fact_id in seen:
            raise WaymarkError(f"duplicate source fact: {fact_id}")
        seen.add(fact_id)
        if fact_id == "goal" and not allow_goal:
            raise WaymarkError("goal cannot be used as an intent source")
        if not fact_exists(conn, fact_id):
            raise WaymarkError(f"unknown source fact: {fact_id}")


def source_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("from", payload.get("sources", payload.get("source_fact_ids")))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise WaymarkError("payload must include `from`, `sources`, or `source_fact_ids` as a list of fact IDs")
    return raw


def description_from_payload(payload: dict[str, Any], *, field: str = "description") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WaymarkError(f"payload must include non-empty `{field}`")
    return value.strip()


def validate_fact_description(description: str) -> None:
    if len(description) > MAX_FACT_DESCRIPTION_CHARS:
        raise WaymarkError(
            f"fact description is {len(description)} chars (max {MAX_FACT_DESCRIPTION_CHARS}); "
            "distill the conclusion and reference long evidence via `evidence_path`"
        )


def evidence_from_payload(payload: dict[str, Any]) -> dict[str, str | None]:
    evidence: dict[str, str | None] = {}
    for field in ("evidence_cmd", "evidence_path"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise WaymarkError(f"`{field}` must be a non-empty string when provided")
        evidence[field] = value.strip() if isinstance(value, str) else None
    return evidence


def create_fact(
    conn: sqlite3.Connection,
    description: str,
    fact_id: str | None = None,
    *,
    evidence_cmd: str | None = None,
    evidence_path: str | None = None,
) -> str:
    validate_fact_description(description)
    new_id = fact_id or next_scoped_id(conn, "fact", "f")
    conn.execute(
        "INSERT INTO facts(id, project_id, description, created_at, evidence_cmd, evidence_path) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id, PROJECT_ID, description, utc_now(), evidence_cmd, evidence_path),
    )
    return new_id


def priority_from_payload(payload: dict[str, Any]) -> int:
    value = payload.get("priority", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WaymarkError("`priority` must be an integer (lower claims first)")
    return value


def create_intent(
    conn: sqlite3.Connection,
    description: str,
    creator: str,
    source_ids: list[str],
    *,
    intent_id: str | None = None,
    to_fact_id: str | None = None,
    worker: str | None = None,
    concluded_at: str | None = None,
    priority: int = 0,
) -> str:
    new_id = intent_id or next_scoped_id(conn, "intent", "i")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO intents(id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id, PROJECT_ID, to_fact_id, description, creator, worker, now if worker and not concluded_at else None, now, concluded_at, priority),
    )
    for index, fact_id in enumerate(source_ids):
        conn.execute(
            "INSERT INTO intent_sources(intent_id, project_id, fact_id, position) VALUES (?, ?, ?, ?)",
            (new_id, PROJECT_ID, fact_id, index),
        )
    return new_id


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_facts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            "SELECT id, description, created_at, evidence_cmd, evidence_path FROM facts WHERE project_id = ? ORDER BY created_at, id",
            (PROJECT_ID,),
        ).fetchall()
    )


def get_hints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            "SELECT id, content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at, id",
            (PROJECT_ID,),
        ).fetchall()
    )


def sources_by_intent(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT intent_id, fact_id FROM intent_sources WHERE project_id = ? ORDER BY intent_id, position",
        (PROJECT_ID,),
    ).fetchall()
    sources: dict[str, list[str]] = {}
    for row in rows:
        sources.setdefault(row["intent_id"], []).append(row["fact_id"])
    return sources


def get_intents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at,
               priority, attempt_count, release_count, last_release_reason, abandoned_at
        FROM intents WHERE project_id = ? ORDER BY created_at, id
        """,
        (PROJECT_ID,),
    ).fetchall()
    sources = sources_by_intent(conn)
    intents: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["sources"] = sources.get(row["id"], [])
        intents.append(data)
    return intents


def get_criteria(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, description, created_at FROM criteria WHERE project_id = ? ORDER BY id",
        (PROJECT_ID,),
    ).fetchall()
    link_rows = conn.execute(
        "SELECT criterion_id, fact_id FROM criterion_facts WHERE project_id = ? ORDER BY criterion_id, position",
        (PROJECT_ID,),
    ).fetchall()
    links: dict[str, list[str]] = {}
    for row in link_rows:
        links.setdefault(row["criterion_id"], []).append(row["fact_id"])
    criteria: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["fact_ids"] = links.get(row["id"], [])
        criteria.append(data)
    return criteria


def count_where(conn: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",
        params,
    ).fetchone()["count"]


def open_intent_count(conn: sqlite3.Connection) -> int:
    return count_where(
        conn, "intents", "project_id = ? AND to_fact_id IS NULL AND abandoned_at IS NULL", (PROJECT_ID,)
    )


def last_semantic_event_seq(conn: sqlite3.Connection) -> int:
    placeholders = ", ".join("?" for _ in NON_SEMANTIC_EVENT_TYPES)
    row = conn.execute(
        f"SELECT MAX(seq) AS seq FROM events WHERE project_id = ? AND type NOT IN ({placeholders})",
        (PROJECT_ID, *NON_SEMANTIC_EVENT_TYPES),
    ).fetchone()
    return row["seq"] or 0


def graph_data(conn: sqlite3.Connection) -> dict[str, Any]:
    project = dict(require_project(conn))
    cfg = dict(settings(conn))
    return {
        "project": project,
        "settings": cfg,
        "facts": get_facts(conn),
        "hints": get_hints(conn),
        "criteria": get_criteria(conn),
        "intents": get_intents(conn),
    }


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\-\n{}\[\],&*?]|^\s|\s$", text):
        return json.dumps(text)
    return text


def to_yaml(data: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(value)}")
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return f"{pad}[]"
        lines = []
        for item in data:
            if isinstance(item, dict):
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{yaml_scalar(data)}"


def timeline_text(conn: sqlite3.Connection) -> str:
    lines: list[str] = []
    for fact in get_facts(conn):
        lines.append(f"{fact['created_at']} FACT {fact['id']} {fact['description']}")
    for intent in get_intents(conn):
        if intent["to_fact_id"]:
            status = "closed"
        elif intent["abandoned_at"]:
            status = "abandoned"
        else:
            status = "open"
        sources = ",".join(intent["sources"])
        target = intent["to_fact_id"] or "-"
        lines.append(
            f"{intent['created_at']} INTENT {intent['id']} {status} sources=[{sources}] target={target} {intent['description']}"
        )
    for hint in get_hints(conn):
        lines.append(f"{hint['created_at']} HINT {hint['id']} creator={hint['creator']} {hint['content']}")
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def markdown_export(conn: sqlite3.Connection) -> str:
    data = graph_data(conn)
    lines = [
        f"# {data['project']['title']}",
        "",
        f"Status: `{data['project']['status']}`",
        "",
        "## Facts",
        "",
    ]
    for fact in data["facts"]:
        lines.append(f"- `{fact['id']}` {fact['description']}")
    lines += ["", "## Intents", ""]
    for intent in data["intents"]:
        target = intent["to_fact_id"] or "open"
        sources = ", ".join(f"`{item}`" for item in intent["sources"])
        lines.append(f"- `{intent['id']}` -> `{target}` from {sources}: {intent['description']}")
    lines += ["", "## Hints", ""]
    for hint in data["hints"]:
        lines.append(f"- `{hint['id']}` `{hint['creator']}` {hint['content']}")
    return "\n".join(lines) + "\n"


def should_reason(project: sqlite3.Row, facts: int, hints: int, open_intents: int) -> bool:
    """Cairn-style reason re-trigger: dispatch reason only on genuinely new
    information, never because reason's own intent creation changed the graph.

    True when reason has never been claimed, when facts or hints grew since the
    last reason claim, or when open intents drained to zero.
    """
    if project["status"] != "active":
        return False
    if project["last_reason_fact_count"] is None:
        return True
    if facts > project["last_reason_fact_count"]:
        return True
    if hints > (project["last_reason_hint_count"] or 0):
        return True
    if open_intents == 0 and (project["last_reason_open_intent_count"] or 0) > 0:
        return True
    return False


def checkpoint_data(conn: sqlite3.Connection) -> dict[str, Any]:
    facts = count_where(conn, "facts", "project_id = ?", (PROJECT_ID,))
    hints = count_where(conn, "hints", "project_id = ?", (PROJECT_ID,))
    open_intents = open_intent_count(conn)
    abandoned_intents = count_where(
        conn, "intents", "project_id = ? AND to_fact_id IS NULL AND abandoned_at IS NOT NULL", (PROJECT_ID,)
    )
    criteria_count = count_where(conn, "criteria", "project_id = ?", (PROJECT_ID,))
    project = require_project(conn)
    cfg = settings(conn)
    bootstrap_result = project["bootstrap_result"]
    rounds_without_progress = project["rounds_without_progress"]
    return {
        "project_id": PROJECT_ID,
        "status": project["status"],
        "bootstrap_enabled": bool(project["bootstrap_enabled"]),
        "bootstrap_attempted": bootstrap_result is not None,
        "bootstrap_result": bootstrap_result,
        "bootstrap_worker": project["bootstrap_worker"],
        "fact_count": facts,
        "hint_count": hints,
        "open_intent_count": open_intents,
        "abandoned_intent_count": abandoned_intents,
        "criteria_count": criteria_count,
        "last_event_seq": last_semantic_event_seq(conn),
        "reason_worker": project["reason_worker"],
        "reason_trigger": project["reason_trigger"],
        "should_reason": should_reason(project, facts, hints, open_intents),
        "round_count": project["round_count"],
        "rounds_without_progress": rounds_without_progress,
        "max_rounds": cfg["max_rounds"],
        "should_handoff": project["status"] == "active" and rounds_without_progress >= cfg["max_rounds"],
    }


def render_state(ctx: Context, conn: sqlite3.Connection) -> None:
    checkpoint = checkpoint_data(conn)
    template = read_template("STATE.md")
    text = template.format(
        run_dir=str(ctx.run),
        project_id=PROJECT_ID,
        status=checkpoint["status"],
        bootstrap_enabled=str(checkpoint["bootstrap_enabled"]).lower(),
        bootstrap_attempted=str(checkpoint["bootstrap_attempted"]).lower(),
        bootstrap_result=checkpoint["bootstrap_result"] or "null",
        bootstrap_worker=checkpoint["bootstrap_worker"] or "null",
        round_count=checkpoint["round_count"],
        rounds_without_progress=checkpoint["rounds_without_progress"],
        max_rounds=checkpoint["max_rounds"],
        checkpoint_json=json_dumps(checkpoint),
    )
    (ctx.run / "STATE.md").write_text(text, encoding="utf-8")


def read_template(name: str) -> str:
    plugin_root = Path(__file__).resolve().parents[1]
    return (plugin_root / "templates" / name).read_text(encoding="utf-8")


def render_views(ctx: Context, conn: sqlite3.Connection) -> None:
    data = graph_data(conn)
    (ctx.run / "graph.yaml").write_text(to_yaml(data) + "\n", encoding="utf-8")
    (ctx.run / "timeline.txt").write_text(timeline_text(conn), encoding="utf-8")
    (ctx.run / "reports").mkdir(exist_ok=True)
    (ctx.run / "reports" / "final.md").write_text(markdown_export(conn), encoding="utf-8")
    render_state(ctx, conn)


def criteria_audit(conn: sqlite3.Connection, facts: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Check that every acceptance criterion is mapped to at least one existing,
    non-goal fact. Returns (criteria with satisfied flags, errors)."""
    errors: list[str] = []
    criteria = get_criteria(conn)
    for criterion in criteria:
        fact_ids = criterion["fact_ids"]
        valid_ids = [fact_id for fact_id in fact_ids if fact_id in facts and fact_id != "goal"]
        criterion["satisfied"] = bool(valid_ids)
        if not fact_ids:
            errors.append(f"criterion {criterion['id']} has no supporting fact")
        elif not valid_ids:
            errors.append(f"criterion {criterion['id']} maps only to missing or goal facts")
    return criteria, errors


def audit_data(conn: sqlite3.Connection) -> dict[str, Any]:
    errors: list[str] = []
    project = dict(require_project(conn))
    facts = {fact["id"]: fact for fact in get_facts(conn)}
    completion_intents = [
        intent for intent in get_intents(conn) if intent["to_fact_id"] == "goal"
    ]
    if project["status"] != "completed":
        errors.append("project is not completed")
    if len(completion_intents) > 1:
        errors.append("multiple completion intents point to goal")
    completion = completion_intents[-1] if completion_intents else None
    completion_info: dict[str, Any] = {"present": completion is not None, "valid": False}
    if completion is None:
        errors.append("completion intent is missing")
    elif completion["concluded_at"] is None:
        errors.append("completion intent is not concluded")
    if completion is not None:
        sources = completion["sources"]
        missing = [fact_id for fact_id in sources if fact_id not in facts]
        goal_sources = [fact_id for fact_id in sources if fact_id == "goal"]
        if not sources:
            errors.append("completion intent has no supporting sources")
        if missing:
            errors.append(f"completion intent has missing sources: {', '.join(missing)}")
        if goal_sources:
            errors.append("completion intent uses goal as a source")
        completion_info = {
            "present": True,
            "valid": project["status"] == "completed" and completion["concluded_at"] is not None and not missing and not goal_sources and bool(sources),
            "intent_id": completion["id"],
            "goal_fact_id": completion["to_fact_id"],
            "supporting_fact_ids": sources,
            "description": completion["description"],
            "worker": completion["worker"],
        }
    # Criteria gate completion proof only; while the project is active their
    # mappings are legitimately empty, and doctor's not_completed_yet detection
    # relies on active-project audits carrying exactly the two base errors.
    criteria = get_criteria(conn)
    if project["status"] == "completed":
        criteria, criteria_errors = criteria_audit(conn, facts)
        errors.extend(criteria_errors)
    return {
        "ok": not errors,
        "errors": errors,
        "project": {"id": project["id"], "title": project["title"], "status": project["status"]},
        "completion": completion_info,
        "criteria": criteria,
        "checkpoint": checkpoint_data(conn),
    }


def post_mutation(ctx: Context, conn: sqlite3.Connection) -> None:
    render_views(ctx, conn)


def capture_baseline_ref() -> str | None:
    """Best-effort git HEAD capture so completion can be verified against the
    repository state the run started from."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    return ref or None


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    ctx = ensure_run_dir(Path(args.run))
    if ctx.db_path.exists() and not args.force:
        raise StorageError(f"blackboard already exists: {ctx.db_path}")
    baseline_ref = capture_baseline_ref()
    with connect(ctx) as conn:
        with transaction(conn):
            init_schema(conn)
            now = utc_now()
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM scoped_counters")
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM verification_runs")
            conn.execute("DELETE FROM criterion_facts")
            conn.execute("DELETE FROM criteria")
            conn.execute("DELETE FROM intent_sources")
            conn.execute("DELETE FROM intents")
            conn.execute("DELETE FROM hints")
            conn.execute("DELETE FROM facts")
            conn.execute("DELETE FROM projects")
            conn.execute(
                """
                INSERT INTO projects(
                    id,
                    title,
                    status,
                    bootstrap_enabled,
                    bootstrap_attempted_at,
                    bootstrap_worker,
                    bootstrap_result,
                    created_at,
                    baseline_ref
                )
                VALUES (?, ?, 'active', ?, NULL, NULL, NULL, ?, ?)
                """,
                (PROJECT_ID, args.title, 0 if args.no_bootstrap else 1, now, baseline_ref),
            )
            conn.execute(
                "INSERT INTO settings(id, intent_timeout, reason_timeout, max_intents, max_rounds) VALUES (1, ?, ?, ?, ?)",
                (args.intent_timeout, args.reason_timeout, args.max_intents, args.max_rounds),
            )
            create_fact(conn, args.origin, "origin")
            create_fact(conn, args.goal, "goal")
            criterion_ids: list[str] = []
            for criterion in args.criterion or []:
                if not criterion.strip():
                    raise WaymarkError("criteria must be non-empty strings")
                criterion_id = next_scoped_id(conn, "criterion", "c")
                conn.execute(
                    "INSERT INTO criteria(id, project_id, description, created_at) VALUES (?, ?, ?, ?)",
                    (criterion_id, PROJECT_ID, criterion.strip(), utc_now()),
                )
                criterion_ids.append(criterion_id)
            for hint in args.hint or []:
                hint_id = next_scoped_id(conn, "hint", "h")
                conn.execute(
                    "INSERT INTO hints(id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hint_id, PROJECT_ID, hint, "init", utc_now()),
                )
            append_event(
                conn,
                ctx,
                "project_initialized",
                {
                    "title": args.title,
                    "origin": args.origin,
                    "goal": args.goal,
                    "bootstrap_enabled": not args.no_bootstrap,
                    "criteria": criterion_ids,
                    "baseline_ref": baseline_ref,
                },
            )
            objective = f"# Objective\n\n## Origin\n\n{args.origin}\n\n## Goal\n\n{args.goal}\n"
            if criterion_ids:
                criteria_lines = "\n".join(
                    f"- `{criterion_id}` {text.strip()}"
                    for criterion_id, text in zip(criterion_ids, args.criterion)
                )
                objective += f"\n## Acceptance Criteria\n\n{criteria_lines}\n"
            (ctx.run / "OBJECTIVE.md").write_text(objective, encoding="utf-8")
            protocol = read_template("PROTOCOL.md").format(run_dir=str(ctx.run), project_id=PROJECT_ID)
            (ctx.run / "PROTOCOL.md").write_text(protocol, encoding="utf-8")
            post_mutation(ctx, conn)
        return {
            "run": str(ctx.run),
            "project_id": PROJECT_ID,
            "status": "active",
            "criteria": criterion_ids,
            "baseline_ref": baseline_ref,
        }


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        return graph_data(conn)["project"]


def cmd_graph(args: argparse.Namespace) -> str | dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        data = graph_data(conn)
    if args.format == "json":
        return json_dumps(data) + "\n"
    return to_yaml(data) + "\n"


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
            append_event(conn, ctx, "snapshot", {"path": str(ctx.run / "graph.yaml")})
        return {"graph": str(ctx.run / "graph.yaml"), "timeline": str(ctx.run / "timeline.txt")}


def cmd_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        return checkpoint_data(conn)


def cmd_hint_add(args: argparse.Namespace) -> dict[str, Any]:
    payload = read_json_stdin()
    content = description_from_payload(payload, field="content")
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            project = require_project(conn)
            if project["status"] not in {"active", "completed", "stopped"}:
                raise InactiveProjectError(f"project is {project['status']}")
            hint_id = next_scoped_id(conn, "hint", "h")
            conn.execute(
                "INSERT INTO hints(id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                (hint_id, PROJECT_ID, content, args.creator, utc_now()),
            )
            append_event(conn, ctx, "hint_added", {"hint_id": hint_id, "creator": args.creator, "content": content})
            post_mutation(ctx, conn)
            return {"hint_id": hint_id}


def cmd_intent_create(args: argparse.Namespace) -> dict[str, Any]:
    payload = normalize_worker_result(read_json_stdin())
    source_ids = source_ids_from_payload(payload)
    description = description_from_payload(payload)
    priority = priority_from_payload(payload)
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            validate_sources(conn, source_ids)
            cfg = settings(conn)
            open_intents = open_intent_count(conn)
            if open_intents >= cfg["max_intents"]:
                raise NoopError(
                    f"open intent cap reached ({open_intents}/{cfg['max_intents']}); "
                    "conclude or abandon existing intents before creating more"
                )
            intent_id = create_intent(conn, description, args.creator, source_ids, priority=priority)
            append_event(
                conn,
                ctx,
                "intent_created",
                {
                    "intent_id": intent_id,
                    "creator": args.creator,
                    "sources": source_ids,
                    "description": description,
                    "priority": priority,
                },
            )
            post_mutation(ctx, conn)
            return {"intent_id": intent_id, "sources": source_ids, "priority": priority}


def pick_claimable_intent(conn: sqlite3.Connection, requested: str | None) -> sqlite3.Row | None:
    if requested:
        return conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND id = ? AND to_fact_id IS NULL",
            (PROJECT_ID, requested),
        ).fetchone()
    # FIFO with priority override (lower wins) so old intents cannot starve
    # behind a stream of newly created ones.
    return conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = ? AND to_fact_id IS NULL AND abandoned_at IS NULL
        ORDER BY priority ASC, created_at ASC, id ASC
        LIMIT 1
        """,
        (PROJECT_ID,),
    ).fetchone()


def cmd_intent_claim(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            intent = pick_claimable_intent(conn, args.intent)
            if intent is None:
                raise NoopError("no open intent to claim")
            if intent["abandoned_at"]:
                raise NoopError(f"intent {intent['id']} is abandoned after {intent['release_count']} failed attempts")
            if intent["worker"] and intent["worker"] != args.worker:
                raise LeaseConflictError(f"intent {intent['id']} is claimed by {intent['worker']}")
            now = utc_now()
            fresh_claim = intent["worker"] is None
            conn.execute(
                """
                UPDATE intents
                SET worker = ?, last_heartbeat_at = ?, attempt_count = attempt_count + ?
                WHERE project_id = ? AND id = ?
                """,
                (args.worker, now, 1 if fresh_claim else 0, PROJECT_ID, intent["id"]),
            )
            append_event(conn, ctx, "intent_claimed", {"intent_id": intent["id"], "worker": args.worker})
            post_mutation(ctx, conn)
            return {
                "intent_id": intent["id"],
                "worker": args.worker,
                "description": intent["description"],
                "attempt_count": intent["attempt_count"] + (1 if fresh_claim else 0),
                "release_count": intent["release_count"],
            }


def require_intent(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND id = ?",
        (PROJECT_ID, intent_id),
    ).fetchone()
    if row is None:
        raise WaymarkError(f"unknown intent: {intent_id}")
    return row


def cmd_intent_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            intent = require_intent(conn, args.intent)
            if intent["to_fact_id"]:
                raise NoopError(f"intent {args.intent} is concluded")
            if intent["worker"] and intent["worker"] != args.worker:
                raise LeaseConflictError(f"intent {args.intent} is claimed by {intent['worker']}")
            conn.execute(
                "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE project_id = ? AND id = ?",
                (args.worker, utc_now(), PROJECT_ID, args.intent),
            )
            append_event(conn, ctx, "intent_heartbeat", {"intent_id": args.intent, "worker": args.worker})
            # Heartbeats only refresh a timestamp; regenerating every view file
            # for them is O(graph) work per beat. Views catch up on the next
            # semantic mutation or read command.
            return {"intent_id": args.intent, "worker": args.worker}


def cmd_intent_release(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            intent = require_intent(conn, args.intent)
            if intent["to_fact_id"]:
                raise NoopError(f"intent {args.intent} is concluded")
            if intent["worker"] and intent["worker"] != args.worker:
                raise LeaseConflictError(f"intent {args.intent} is claimed by {intent['worker']}")
            changed = bool(intent["worker"])
            outcome = {"release_count": intent["release_count"], "abandoned": bool(intent["abandoned_at"])}
            if changed:
                outcome = release_intent_lease(conn, ctx, args.intent, args.reason or "released")
            append_event(
                conn,
                ctx,
                "intent_released",
                {
                    "intent_id": args.intent,
                    "worker": args.worker,
                    "changed": changed,
                    "reason": args.reason,
                    "release_count": outcome["release_count"],
                    "abandoned": outcome["abandoned"],
                },
            )
            post_mutation(ctx, conn)
            return {
                "intent_id": args.intent,
                "released": changed,
                "release_count": outcome["release_count"],
                "abandoned": outcome["abandoned"],
            }


def cmd_intent_conclude(args: argparse.Namespace) -> dict[str, Any]:
    payload = normalize_worker_result(read_json_stdin())
    description = description_from_payload(payload)
    evidence = evidence_from_payload(payload)
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            intent = require_intent(conn, args.intent)
            if intent["to_fact_id"]:
                raise NoopError(f"intent {args.intent} is already concluded")
            if intent["worker"] != args.worker:
                raise LeaseConflictError(f"intent {args.intent} is not claimed by {args.worker}")
            fact_id = create_fact(
                conn,
                description,
                evidence_cmd=evidence["evidence_cmd"],
                evidence_path=evidence["evidence_path"],
            )
            now = utc_now()
            conn.execute(
                """
                UPDATE intents
                SET to_fact_id = ?, worker = ?, last_heartbeat_at = NULL, concluded_at = ?
                WHERE project_id = ? AND id = ?
                """,
                (fact_id, args.worker, now, PROJECT_ID, args.intent),
            )
            append_event(
                conn,
                ctx,
                "intent_concluded",
                {"intent_id": args.intent, "worker": args.worker, "to_fact_id": fact_id, "description": description},
            )
            post_mutation(ctx, conn)
            return {"intent_id": args.intent, "to_fact_id": fact_id}


def cmd_reason_claim(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            if project["reason_worker"] and project["reason_worker"] != args.worker:
                raise LeaseConflictError(f"reason lease is held by {project['reason_worker']}")
            now = utc_now()
            # Record the graph fingerprint at claim time; checkpoint.should_reason
            # re-triggers reason only when facts/hints grow past this baseline or
            # open intents drain to zero — never on reason's own intent creation.
            facts = count_where(conn, "facts", "project_id = ?", (PROJECT_ID,))
            hints = count_where(conn, "hints", "project_id = ?", (PROJECT_ID,))
            open_intents = open_intent_count(conn)
            conn.execute(
                """
                UPDATE projects
                SET reason_worker = ?, reason_trigger = ?, reason_started_at = COALESCE(reason_started_at, ?), reason_last_heartbeat_at = ?,
                    last_reason_fact_count = ?, last_reason_hint_count = ?, last_reason_open_intent_count = ?
                WHERE id = ?
                """,
                (args.worker, args.trigger, now, now, facts, hints, open_intents, PROJECT_ID),
            )
            append_event(conn, ctx, "reason_claimed", {"worker": args.worker, "trigger": args.trigger})
            post_mutation(ctx, conn)
            return {"worker": args.worker, "trigger": args.trigger}


def cmd_reason_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            if project["reason_worker"] != args.worker:
                raise LeaseConflictError("reason lease is not held by this worker")
            conn.execute(
                "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
                (utc_now(), PROJECT_ID),
            )
            append_event(conn, ctx, "reason_heartbeat", {"worker": args.worker})
            # Same as intent-heartbeat: no view regeneration for a timestamp bump.
            return {"worker": args.worker}


def cmd_reason_release(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            if project["reason_worker"] and project["reason_worker"] != args.worker:
                raise LeaseConflictError(f"reason lease is held by {project['reason_worker']}")
            changed = bool(project["reason_worker"])
            conn.execute(
                """
                UPDATE projects
                SET reason_worker = NULL, reason_trigger = NULL, reason_started_at = NULL, reason_last_heartbeat_at = NULL
                WHERE id = ?
                """,
                (PROJECT_ID,),
            )
            append_event(conn, ctx, "reason_released", {"worker": args.worker, "changed": changed})
            post_mutation(ctx, conn)
            return {"released": changed}


def criteria_mapping_from_payload(payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = payload.get("criteria")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WaymarkError("`criteria` must be an object mapping criterion IDs to fact IDs")
    mapping: dict[str, list[str]] = {}
    for criterion_id, value in raw.items():
        fact_ids = [value] if isinstance(value, str) else value
        if not isinstance(fact_ids, list) or not fact_ids or not all(isinstance(item, str) and item for item in fact_ids):
            raise WaymarkError(f"criteria mapping for `{criterion_id}` must be a fact ID or non-empty list of fact IDs")
        mapping[criterion_id] = fact_ids
    return mapping


def apply_criteria_mapping(conn: sqlite3.Connection, mapping: dict[str, list[str]]) -> None:
    """Validate and persist criterion→fact links. Every defined criterion must
    be covered; completion without a full mapping is not falsifiable."""
    criteria = {criterion["id"] for criterion in get_criteria(conn)}
    unknown = sorted(set(mapping) - criteria)
    if unknown:
        raise WaymarkError(f"unknown criteria in mapping: {', '.join(unknown)}")
    unmapped = sorted(criteria - set(mapping))
    if unmapped:
        raise WaymarkError(
            f"completion must map every acceptance criterion to supporting facts; unmapped: {', '.join(unmapped)}"
        )
    conn.execute("DELETE FROM criterion_facts WHERE project_id = ?", (PROJECT_ID,))
    for criterion_id, fact_ids in mapping.items():
        for position, fact_id in enumerate(fact_ids):
            if fact_id == "goal":
                raise WaymarkError(f"criterion {criterion_id} cannot be satisfied by the goal fact")
            if not fact_exists(conn, fact_id):
                raise WaymarkError(f"criterion {criterion_id} maps to unknown fact: {fact_id}")
            conn.execute(
                "INSERT INTO criterion_facts(criterion_id, project_id, fact_id, position) VALUES (?, ?, ?, ?)",
                (criterion_id, PROJECT_ID, fact_id, position),
            )


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    payload = normalize_worker_result(read_json_stdin())
    criteria_mapping = criteria_mapping_from_payload(payload)
    if "complete" in payload and isinstance(payload["complete"], dict):
        payload = payload["complete"]
        criteria_mapping = criteria_mapping or criteria_mapping_from_payload(payload)
    source_ids = source_ids_from_payload(payload)
    description = description_from_payload(payload)
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            validate_sources(conn, source_ids)
            apply_criteria_mapping(conn, criteria_mapping)
            now = utc_now()
            intent_id = create_intent(
                conn,
                description,
                COMPLETION_CREATOR,
                source_ids,
                to_fact_id="goal",
                worker=args.worker,
                concluded_at=now,
            )
            conn.execute(
                """
                UPDATE projects
                SET status = 'completed',
                    reason_worker = NULL,
                    reason_trigger = NULL,
                    reason_started_at = NULL,
                    reason_last_heartbeat_at = NULL
                WHERE id = ?
                """,
                (PROJECT_ID,),
            )
            append_event(
                conn,
                ctx,
                "project_completed",
                {
                    "intent_id": intent_id,
                    "worker": args.worker,
                    "sources": source_ids,
                    "description": description,
                    "criteria": criteria_mapping,
                },
            )
            post_mutation(ctx, conn)
            return {
                "completion_intent_id": intent_id,
                "goal_fact_id": "goal",
                "supporting_fact_ids": source_ids,
                "criteria": criteria_mapping,
            }


def cmd_bootstrap_complete(args: argparse.Namespace) -> dict[str, Any]:
    payload = normalize_worker_result(read_json_stdin())
    fact_payload = payload.get("fact")
    complete_payload = payload.get("complete")
    if not isinstance(fact_payload, dict):
        raise WaymarkError("payload must include object `fact`")
    if not isinstance(complete_payload, dict):
        raise WaymarkError("payload must include object `complete`")
    fact_description = description_from_payload(fact_payload)
    fact_evidence = evidence_from_payload(fact_payload)
    complete_description = description_from_payload(complete_payload)
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            if not project["bootstrap_enabled"]:
                raise WaymarkError("bootstrap is disabled for this project")
            if project["bootstrap_result"] == "noop":
                raise WaymarkError("bootstrap-complete is not allowed after bootstrap-noop")
            if project["bootstrap_result"] == "completed":
                raise InactiveProjectError("bootstrap is already completed")
            bootstrap_fact_id = create_fact(
                conn,
                fact_description,
                evidence_cmd=fact_evidence["evidence_cmd"],
                evidence_path=fact_evidence["evidence_path"],
            )
            # Direct completion has exactly one evidence fact, so every defined
            # criterion is linked to it; the bootstrap evidence must cover them all.
            criteria_mapping = {criterion["id"]: [bootstrap_fact_id] for criterion in get_criteria(conn)}
            apply_criteria_mapping(conn, criteria_mapping)
            now = utc_now()
            bootstrap_intent_id = create_intent(
                conn,
                fact_description,
                "bootstrap",
                ["origin"],
                to_fact_id=bootstrap_fact_id,
                worker=args.worker,
                concluded_at=now,
            )
            completion_intent_id = create_intent(
                conn,
                complete_description,
                COMPLETION_CREATOR,
                [bootstrap_fact_id],
                to_fact_id="goal",
                worker=args.worker,
                concluded_at=now,
            )
            conn.execute(
                """
                UPDATE projects
                SET status = 'completed',
                    bootstrap_attempted_at = ?,
                    bootstrap_worker = ?,
                    bootstrap_result = 'completed',
                    reason_worker = NULL,
                    reason_trigger = NULL,
                    reason_started_at = NULL,
                    reason_last_heartbeat_at = NULL
                WHERE id = ?
                """,
                (now, args.worker, PROJECT_ID),
            )
            append_event(
                conn,
                ctx,
                "bootstrap_completed",
                {
                    "bootstrap_intent_id": bootstrap_intent_id,
                    "completion_intent_id": completion_intent_id,
                    "bootstrap_fact_id": bootstrap_fact_id,
                    "worker": args.worker,
                },
            )
            append_event(
                conn,
                ctx,
                "project_completed",
                {
                    "intent_id": completion_intent_id,
                    "worker": args.worker,
                    "sources": [bootstrap_fact_id],
                    "description": complete_description,
                },
            )
            post_mutation(ctx, conn)
            return {
                "bootstrap_intent_id": bootstrap_intent_id,
                "bootstrap_fact_id": bootstrap_fact_id,
                "bootstrap_attempted": True,
                "bootstrap_result": "completed",
                "completion_intent_id": completion_intent_id,
                "goal_fact_id": "goal",
                "supporting_fact_ids": [bootstrap_fact_id],
            }


def cmd_bootstrap_noop(args: argparse.Namespace) -> dict[str, Any]:
    payload = normalize_worker_result(read_json_stdin())
    reason = description_from_payload(payload, field="reason")
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            if not project["bootstrap_enabled"]:
                raise WaymarkError("bootstrap is disabled for this project")
            if project["bootstrap_result"] == "completed":
                raise InactiveProjectError("bootstrap is already completed")
            if project["bootstrap_result"] == "noop":
                post_mutation(ctx, conn)
                return {
                    "bootstrap_attempted": True,
                    "bootstrap_result": "noop",
                    "worker": project["bootstrap_worker"],
                }
            now = utc_now()
            conn.execute(
                """
                UPDATE projects
                SET bootstrap_attempted_at = ?,
                    bootstrap_worker = ?,
                    bootstrap_result = 'noop'
                WHERE id = ?
                """,
                (now, args.worker, PROJECT_ID),
            )
            append_event(
                conn,
                ctx,
                "bootstrap_noop",
                {"worker": args.worker, "reason": reason},
            )
            post_mutation(ctx, conn)
            return {
                "bootstrap_attempted": True,
                "bootstrap_result": "noop",
                "worker": args.worker,
            }


def cmd_reopen(args: argparse.Namespace) -> dict[str, Any]:
    payload = read_json_stdin()
    feedback = description_from_payload(payload, field="description")
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            project = require_project(conn)
            if project["status"] != "completed":
                raise NoopError("project is not completed")
            completion = conn.execute(
                """
                SELECT * FROM intents
                WHERE project_id = ? AND to_fact_id = 'goal'
                ORDER BY concluded_at DESC, created_at DESC
                LIMIT 1
                """,
                (PROJECT_ID,),
            ).fetchone()
            if completion is None:
                raise StorageError("completed project has no completion intent")
            source_rows = conn.execute(
                """
                SELECT fact_id FROM intent_sources
                WHERE project_id = ? AND intent_id = ?
                ORDER BY position
                """,
                (PROJECT_ID, completion["id"]),
            ).fetchall()
            old_sources = [row["fact_id"] for row in source_rows]
            conn.execute("DELETE FROM intents WHERE project_id = ? AND id = ?", (PROJECT_ID, completion["id"]))
            # The disproven completion's criteria mapping and verification
            # verdicts are invalid evidence; the next completion must re-map
            # every criterion and be re-verified from scratch.
            conn.execute("DELETE FROM criterion_facts WHERE project_id = ?", (PROJECT_ID,))
            conn.execute("DELETE FROM verification_runs WHERE project_id = ?", (PROJECT_ID,))
            feedback_fact = create_fact(conn, f"external_feedback: {feedback}")
            feedback_intent = create_intent(
                conn,
                feedback,
                args.creator,
                old_sources,
                to_fact_id=feedback_fact,
                worker=args.creator,
                concluded_at=utc_now(),
            )
            conn.execute(
                """
                UPDATE projects
                SET status = 'active',
                    reason_worker = NULL,
                    reason_trigger = NULL,
                    reason_started_at = NULL,
                    reason_last_heartbeat_at = NULL
                WHERE id = ?
                """,
                (PROJECT_ID,),
            )
            append_event(
                conn,
                ctx,
                "project_reopened",
                {
                    "removed_completion_intent_id": completion["id"],
                    "feedback_fact_id": feedback_fact,
                    "external_feedback_intent_id": feedback_intent,
                    "creator": args.creator,
                },
            )
            post_mutation(ctx, conn)
            return {"feedback_fact_id": feedback_fact, "external_feedback_intent_id": feedback_intent}


def cmd_round_start(args: argparse.Namespace) -> dict[str, Any]:
    """Open a supervisor dispatch round and measure progress since the last one.

    Progress means the semantic event sequence advanced — facts, hints, intents
    concluded/abandoned, completion, reopen. Lease churn and heartbeats do not
    count. The protocol hands off when rounds_without_progress reaches
    settings.max_rounds, replacing the previously undefined stall rule.
    """
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            require_active(conn)
            project = require_project(conn)
            cfg = settings(conn)
            current_seq = last_semantic_event_seq(conn)
            if project["round_count"] > 0 and current_seq <= (project["last_round_seq"] or 0):
                rounds_without_progress = project["rounds_without_progress"] + 1
            else:
                rounds_without_progress = 0
            round_count = project["round_count"] + 1
            conn.execute(
                """
                UPDATE projects
                SET round_count = ?, rounds_without_progress = ?, last_round_seq = ?
                WHERE id = ?
                """,
                (round_count, rounds_without_progress, current_seq, PROJECT_ID),
            )
            append_event(
                conn,
                ctx,
                "round_started",
                {
                    "round": round_count,
                    "rounds_without_progress": rounds_without_progress,
                    "last_event_seq": current_seq,
                },
            )
            post_mutation(ctx, conn)
            return {
                "round_count": round_count,
                "rounds_without_progress": rounds_without_progress,
                "max_rounds": cfg["max_rounds"],
                "should_handoff": rounds_without_progress >= cfg["max_rounds"],
                "last_event_seq": current_seq,
            }


def fact_summary(description: str, limit: int = 120) -> str:
    text = " ".join(description.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cmd_brief(args: argparse.Namespace) -> dict[str, Any]:
    """Scoped reason-worker context: checkpoint, objective facts, open/abandoned
    intents, criteria, recent facts, and a one-line index of every fact — instead
    of the full graph dump."""
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
        facts = get_facts(conn)
        fact_by_id = {fact["id"]: fact for fact in facts}
        intents = get_intents(conn)
        open_intents = [
            {
                "id": intent["id"],
                "description": intent["description"],
                "sources": intent["sources"],
                "priority": intent["priority"],
                "worker": intent["worker"],
                "attempt_count": intent["attempt_count"],
                "release_count": intent["release_count"],
                "last_release_reason": intent["last_release_reason"],
            }
            for intent in intents
            if intent["to_fact_id"] is None and not intent["abandoned_at"]
        ]
        abandoned_intents = [
            {
                "id": intent["id"],
                "description": intent["description"],
                "sources": intent["sources"],
                "release_count": intent["release_count"],
                "last_release_reason": intent["last_release_reason"],
            }
            for intent in intents
            if intent["to_fact_id"] is None and intent["abandoned_at"]
        ]
        recent_count = max(args.facts, 0)
        recent = [fact for fact in facts if fact["id"] not in SPECIAL_FACTS][-recent_count:] if recent_count else []
        return {
            "checkpoint": checkpoint_data(conn),
            "origin": fact_by_id.get("origin"),
            "goal": fact_by_id.get("goal"),
            "criteria": get_criteria(conn),
            "open_intents": open_intents,
            "abandoned_intents": abandoned_intents,
            "recent_facts": recent,
            "fact_index": [
                {"id": fact["id"], "summary": fact_summary(fact["description"])}
                for fact in facts
            ],
            "hints": get_hints(conn),
            "settings": dict(settings(conn)),
        }


def cmd_context(args: argparse.Namespace) -> dict[str, Any]:
    """Scoped explore-worker context: one intent, its source facts, and the goal
    — not the whole graph."""
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
        intent_row = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND id = ?",
            (PROJECT_ID, args.intent),
        ).fetchone()
        if intent_row is None:
            raise WaymarkError(f"unknown intent: {args.intent}")
        intent = dict(intent_row)
        source_rows = conn.execute(
            """
            SELECT f.* FROM intent_sources s
            JOIN facts f ON f.id = s.fact_id AND f.project_id = s.project_id
            WHERE s.project_id = ? AND s.intent_id = ?
            ORDER BY s.position
            """,
            (PROJECT_ID, args.intent),
        ).fetchall()
        intent["sources"] = [row["id"] for row in source_rows]
        goal = conn.execute(
            "SELECT * FROM facts WHERE project_id = ? AND id = 'goal'",
            (PROJECT_ID,),
        ).fetchone()
        return {
            "intent": intent,
            "source_facts": rows_to_dicts(source_rows),
            "goal": dict(goal) if goal else None,
            "hints": get_hints(conn),
            "open_intent_count": open_intent_count(conn),
            "settings": dict(settings(conn)),
        }


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic completion-evidence report for the verifier worker.

    Checks evidence paths on disk and lists evidence commands for the verifier
    to execute; the CLI never runs fact-supplied commands itself. Coverage
    distinguishes re-verifiable evidence from trust-prior facts.
    """
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
        audit = audit_data(conn)
        project = dict(require_project(conn))
        facts = {fact["id"]: fact for fact in get_facts(conn)}
        supporting_ids: list[str] = []
        if audit["completion"].get("supporting_fact_ids"):
            supporting_ids.extend(audit["completion"]["supporting_fact_ids"])
        for criterion in audit["criteria"]:
            for fact_id in criterion["fact_ids"]:
                if fact_id not in supporting_ids:
                    supporting_ids.append(fact_id)
        supporting: list[dict[str, Any]] = []
        paths_ok = 0
        paths_missing = 0
        with_cmd = 0
        for fact_id in supporting_ids:
            fact = facts.get(fact_id)
            if fact is None:
                continue
            entry = dict(fact)
            if fact["evidence_path"]:
                exists = Path(fact["evidence_path"]).exists()
                entry["evidence_path_exists"] = exists
                paths_ok += 1 if exists else 0
                paths_missing += 0 if exists else 1
            else:
                entry["evidence_path_exists"] = None
            if fact["evidence_cmd"]:
                with_cmd += 1
            supporting.append(entry)
        verifiable = sum(1 for fact in supporting if fact["evidence_path"] or fact["evidence_cmd"])
        return {
            "audit_ok": audit["ok"],
            "audit_errors": audit["errors"],
            "baseline_ref": project["baseline_ref"],
            "supporting_facts": supporting,
            "criteria": audit["criteria"],
            "coverage": {
                "facts_total": len(supporting),
                "re_verifiable": verifiable,
                "trust_prior": len(supporting) - verifiable,
                "with_evidence_cmd": with_cmd,
                "paths_ok": paths_ok,
                "paths_missing": paths_missing,
            },
        }


def latest_verification(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM verification_runs WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (PROJECT_ID,),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["verified"] = bool(record["verified"])
    return record


def cmd_verification_record(args: argparse.Namespace) -> dict[str, Any]:
    """Persist a verifier verdict so verification survives the transcript.

    Accepts both worker verdict shapes: a failed verification arrives as
    `accepted=false` with `verified=false` inside `data`, and must still be
    recordable — a durable failure is the point.
    """
    payload = read_json_stdin()
    if "accepted" in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    verified = payload.get("verified")
    if not isinstance(verified, bool):
        raise WaymarkError("payload must include boolean `verified`")
    for field in ("evidence", "reason"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise WaymarkError(f"`{field}` must be a string when provided")
    for field in ("re_verified", "trust_prior"):
        value = payload.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise WaymarkError(f"`{field}` must be a non-negative integer when provided")
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            project = require_project(conn)
            if project["status"] != "completed":
                raise WaymarkError("verification requires a completed project; run it after audit reports ok")
            record_id = next_scoped_id(conn, "verification", "v")
            conn.execute(
                """
                INSERT INTO verification_runs(id, project_id, worker, verified, evidence, reason, re_verified, trust_prior, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    PROJECT_ID,
                    args.worker,
                    1 if verified else 0,
                    payload.get("evidence"),
                    payload.get("reason"),
                    payload.get("re_verified"),
                    payload.get("trust_prior"),
                    utc_now(),
                ),
            )
            append_event(
                conn,
                ctx,
                "verification_recorded",
                {
                    "verification_id": record_id,
                    "worker": args.worker,
                    "verified": verified,
                    "re_verified": payload.get("re_verified"),
                    "trust_prior": payload.get("trust_prior"),
                },
            )
            post_mutation(ctx, conn)
            return {"verification_id": record_id, "verified": verified}


def cmd_final_status(args: argparse.Namespace) -> dict[str, Any]:
    """One consolidated completion authority.

    Combines the structural audit, the criteria check, and the latest durable
    verification record into a single decision object so the supervisor — /goal
    protocol or a workflow script — gates WAYMARK_RUN_COMPLETE on exactly one
    command instead of recombining audit, verify, and transcript output.
    """
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        audit = audit_data(conn)
        checkpoint = audit["checkpoint"]
        record = latest_verification(conn)
        facts = {fact["id"]: fact for fact in get_facts(conn)}
        criteria, _criteria_errors = criteria_audit(conn, facts)
        errors = list(audit["errors"])
        completed = audit["project"]["status"] == "completed"
        if not completed:
            status = "handoff" if checkpoint["should_handoff"] else "not_completed"
        elif not audit["ok"]:
            status = "audit_failed"
        elif record is None:
            status = "verification_missing"
            errors.append("verification record is missing; verifier-worker must run waymark verification-record")
        elif not record["verified"]:
            status = "verification_failed"
            errors.append(f"latest verification failed: {record['reason'] or 'no reason recorded'}")
        else:
            status = "ready"
        return {
            "ready": status == "ready",
            "status": status,
            "audit_ok": audit["ok"],
            "verification_ok": bool(record and record["verified"]),
            "criteria_ok": all(criterion["satisfied"] for criterion in criteria),
            "should_handoff": bool(checkpoint["should_handoff"]),
            "completion_intent_id": audit["completion"].get("intent_id"),
            "verification": record,
            "errors": errors,
        }


def cmd_audit(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        return audit_data(conn)


def cmd_export(args: argparse.Namespace) -> str:
    ctx = require_context(Path(args.run))
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        if args.format == "yaml":
            return (ctx.run / "graph.yaml").read_text(encoding="utf-8")
        if args.format == "timeline":
            return (ctx.run / "timeline.txt").read_text(encoding="utf-8")
        return markdown_export(conn)


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    ctx = require_context(Path(args.run))
    checks: list[dict[str, Any]] = []
    with connect(ctx) as conn:
        with transaction(conn):
            clear_expired_leases(conn, ctx)
            post_mutation(ctx, conn)
        audit = audit_data(conn)
        checks.append({"name": "database", "ok": True, "path": str(ctx.db_path)})
        for name in ["OBJECTIVE.md", "PROTOCOL.md", "STATE.md", "graph.yaml", "timeline.txt", "events.jsonl", "reports/final.md"]:
            checks.append({"name": name, "ok": (ctx.run / name).exists()})
        incomplete_only = audit["errors"] == ["project is not completed", "completion intent is missing"]
        checks.append(
            {
                "name": "audit",
                "ok": audit["ok"],
                "status": "not_completed_yet" if incomplete_only else ("passed" if audit["ok"] else "structural_error"),
                "errors": audit["errors"],
            }
        )
        return {
            "ok": all(check["ok"] for check in checks),
            "status": "ok" if audit["ok"] else ("not_completed_yet" if incomplete_only else "structural_error"),
            "checks": checks,
        }


def add_common_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", required=True, help="Waymark run directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waymark")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    add_common_run(p)
    p.add_argument("--title", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--hint", action="append")
    p.add_argument("--criterion", action="append", help="falsifiable acceptance criterion (repeatable)")
    p.add_argument("--no-bootstrap", action="store_true")
    p.add_argument("--intent-timeout", type=int, default=DEFAULT_INTENT_TIMEOUT)
    p.add_argument("--reason-timeout", type=int, default=DEFAULT_REASON_TIMEOUT)
    p.add_argument("--max-intents", type=int, default=DEFAULT_MAX_INTENTS)
    p.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="consecutive no-progress rounds before the protocol must hand off",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init, json_default=True)

    for name, func in [
        ("status", cmd_status),
        ("snapshot", cmd_snapshot),
        ("checkpoint", cmd_checkpoint),
        ("audit", cmd_audit),
        ("doctor", cmd_doctor),
        ("round-start", cmd_round_start),
        ("verify", cmd_verify),
        ("final-status", cmd_final_status),
    ]:
        p = sub.add_parser(name)
        add_common_run(p)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func, json_default=True)

    p = sub.add_parser("verification-record")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_verification_record, json_default=True)

    p = sub.add_parser("brief")
    add_common_run(p)
    p.add_argument("--facts", type=int, default=DEFAULT_BRIEF_FACTS, help="number of recent facts to include in full")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief, json_default=True)

    p = sub.add_parser("context")
    add_common_run(p)
    p.add_argument("--intent", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context, json_default=True)

    p = sub.add_parser("graph")
    add_common_run(p)
    p.add_argument("--format", choices=["yaml", "json"], default="yaml")
    p.set_defaults(func=cmd_graph, json_default=False)

    p = sub.add_parser("export")
    add_common_run(p)
    p.add_argument("--format", choices=["yaml", "timeline", "markdown"], required=True)
    p.set_defaults(func=cmd_export, json_default=False)

    p = sub.add_parser("hint-add")
    add_common_run(p)
    p.add_argument("--creator", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_hint_add, json_default=True)

    p = sub.add_parser("intent-create")
    add_common_run(p)
    p.add_argument("--creator", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_intent_create, json_default=True)

    p = sub.add_parser("intent-claim")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--intent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_intent_claim, json_default=True)

    p = sub.add_parser("intent-heartbeat")
    add_common_run(p)
    p.add_argument("--intent", required=True)
    p.add_argument("--worker", required=True)
    p.set_defaults(func=cmd_intent_heartbeat, json_default=True)

    p = sub.add_parser("intent-release")
    add_common_run(p)
    p.add_argument("--intent", required=True)
    p.add_argument("--worker", required=True)
    p.add_argument("--reason", help="why the intent could not be concluded (recorded as a strike)")
    p.set_defaults(func=cmd_intent_release, json_default=True)

    p = sub.add_parser("intent-conclude")
    add_common_run(p)
    p.add_argument("--intent", required=True)
    p.add_argument("--worker", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_intent_conclude, json_default=True)

    p = sub.add_parser("reason-claim")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--trigger", required=True)
    p.set_defaults(func=cmd_reason_claim, json_default=True)

    p = sub.add_parser("reason-heartbeat")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.set_defaults(func=cmd_reason_heartbeat, json_default=True)

    p = sub.add_parser("reason-release")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.set_defaults(func=cmd_reason_release, json_default=True)

    p = sub.add_parser("complete")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_complete, json_default=True)

    p = sub.add_parser("bootstrap-complete")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_bootstrap_complete, json_default=True)

    p = sub.add_parser("bootstrap-noop")
    add_common_run(p)
    p.add_argument("--worker", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_bootstrap_noop, json_default=True)

    p = sub.add_parser("reopen")
    add_common_run(p)
    p.add_argument("--creator", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_reopen, json_default=True)

    return parser


def emit_result(result: str | dict[str, Any] | None, as_json: bool) -> None:
    if result is None:
        return
    if isinstance(result, str):
        sys.stdout.write(result)
        return
    if as_json:
        print(json_dumps(result))
    else:
        print(result)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        as_json = bool(getattr(args, "json", False) or getattr(args, "json_default", False))
        emit_result(result, as_json)
        return EXIT_SUCCESS
    except WaymarkError as exc:
        print(f"waymark: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
