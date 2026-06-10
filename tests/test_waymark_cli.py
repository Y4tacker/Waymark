from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "waymark"
HOOK = ROOT / "scripts" / "waymark_hook.py"


class WaymarkCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, run: Path, *args: str, stdin: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [str(CLI), *args]
        input_text = json.dumps(stdin) if stdin is not None else None
        result = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
        if check and result.returncode != 0:
            raise AssertionError(f"{cmd} failed\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def load_json(self, result: subprocess.CompletedProcess) -> dict:
        return json.loads(result.stdout)

    def init_run(self, **kwargs) -> Path:
        run = self.tmp_path / kwargs.get("run_dir", "run")
        args = [
            "init",
            "--run",
            str(run),
            "--title",
            kwargs.get("title", "Test"),
            "--origin",
            kwargs.get("origin", "starting point"),
            "--goal",
            kwargs.get("goal", "finished objective"),
        ]
        if "intent_timeout" in kwargs:
            args.extend(["--intent-timeout", str(kwargs["intent_timeout"])])
        if "reason_timeout" in kwargs:
            args.extend(["--reason-timeout", str(kwargs["reason_timeout"])])
        if "max_intents" in kwargs:
            args.extend(["--max-intents", str(kwargs["max_intents"])])
        if "max_rounds" in kwargs:
            args.extend(["--max-rounds", str(kwargs["max_rounds"])])
        for criterion in kwargs.get("criteria", []):
            args.extend(["--criterion", criterion])
        if kwargs.get("no_bootstrap"):
            args.append("--no-bootstrap")
        self.run_cli(run, *args)
        return run

    def create_intent(self, run: Path, description: str = "explore", priority: int | None = None) -> dict:
        stdin: dict = {"from": ["origin"], "description": description}
        if priority is not None:
            stdin["priority"] = priority
        return self.load_json(
            self.run_cli(run, "intent-create", "--run", str(run), "--creator", "reason-worker", "--stdin", stdin=stdin)
        )

    def complete_run(self, run: Path) -> None:
        """Drive a run to structural completion: one concluded intent, then complete."""
        self.create_intent(run)
        claimed = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1"))
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            claimed["intent_id"],
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "evidence"},
        )
        self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason-worker",
            "--stdin",
            stdin={"from": ["f001"], "description": "done"},
        )

    def record_verification(self, run: Path, stdin: dict) -> subprocess.CompletedProcess:
        return self.run_cli(
            run,
            "verification-record",
            "--run",
            str(run),
            "--worker",
            "verifier-worker",
            "--stdin",
            stdin=stdin,
            check=False,
        )

    def final_status(self, run: Path) -> dict:
        return self.load_json(self.run_cli(run, "final-status", "--run", str(run), "--json"))

    def run_hook(self, event: str, payload: dict, *, strict: bool = False) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(self.tmp_path)
        env.pop("WAYMARK_STRICT_HOOKS", None)
        if strict:
            env["WAYMARK_STRICT_HOOKS"] = "1"
        return subprocess.run(
            [sys.executable, str(HOOK), event],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
        )

    def init_hook_run(self, **kwargs) -> Path:
        """Initialize a run where the hook's active-run discovery looks: .waymark/<run>."""
        return self.init_run(run_dir=Path(".waymark") / "run", **kwargs)

    def db(self, run: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(run / "blackboard.sqlite")
        conn.row_factory = sqlite3.Row
        return conn

    def test_init_creates_origin_and_goal(self) -> None:
        run = self.init_run()
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        facts = {fact["id"]: fact["description"] for fact in graph["facts"]}
        self.assertEqual(facts, {"origin": "starting point", "goal": "finished objective"})
        self.assertTrue((run / "PROTOCOL.md").exists())
        self.assertTrue((run / "STATE.md").exists())
        self.assertTrue((run / "events.jsonl").exists())
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertTrue(checkpoint["bootstrap_enabled"])
        self.assertFalse(checkpoint["bootstrap_attempted"])
        self.assertIsNone(checkpoint["bootstrap_result"])
        self.assertIsNone(checkpoint["bootstrap_worker"])

    def test_audit_active_project_is_not_ok(self) -> None:
        run = self.init_run()
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["project"]["status"], "active")
        self.assertFalse(audit["completion"]["present"])
        self.assertFalse(audit["completion"]["valid"])
        self.assertIn("project is not completed", audit["errors"])

    def test_goal_cannot_be_intent_source(self) -> None:
        run = self.init_run()
        result = self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"from": ["goal"], "description": "bad"},
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("waymark:", result.stderr)
        self.assertIn("goal cannot be used", result.stderr)

    def test_project_scoped_ids_are_stable(self) -> None:
        run = self.init_run()
        hint = self.load_json(
            self.run_cli(
                run,
                "hint-add",
                "--run",
                str(run),
                "--creator",
                "user",
                "--stdin",
                stdin={"content": "hint one"},
            )
        )
        intent = self.load_json(
            self.run_cli(
                run,
                "intent-create",
                "--run",
                str(run),
                "--creator",
                "reason",
                "--stdin",
                stdin={"from": ["origin"], "description": "explore"},
            )
        )
        claim = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1"))
        conclude = self.load_json(
            self.run_cli(
                run,
                "intent-conclude",
                "--run",
                str(run),
                "--intent",
                claim["intent_id"],
                "--worker",
                "w1",
                "--stdin",
                stdin={"description": "new fact"},
            )
        )
        self.assertEqual(hint["hint_id"], "h001")
        self.assertEqual(intent["intent_id"], "i001")
        self.assertEqual(conclude["to_fact_id"], "f001")

    def test_intent_claim_conflicts_and_release_idempotent(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"from": ["origin"], "description": "explore"},
        )
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        conflict = self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w2", "--intent", "i001", check=False)
        self.assertEqual(conflict.returncode, 3)
        released = self.load_json(self.run_cli(run, "intent-release", "--run", str(run), "--intent", "i001", "--worker", "w1"))
        self.assertTrue(released["released"])
        released_again = self.load_json(self.run_cli(run, "intent-release", "--run", str(run), "--intent", "i001", "--worker", "w1"))
        self.assertFalse(released_again["released"])

    def test_bootstrap_complete_creates_fact_and_completion_intent(self) -> None:
        run = self.init_run()
        completed = self.load_json(
            self.run_cli(
                run,
                "bootstrap-complete",
                "--run",
                str(run),
                "--worker",
                "bootstrap-worker",
                "--stdin",
                stdin={
                    "fact": {"description": "direct evidence"},
                    "complete": {"description": "goal satisfied directly"},
                },
            )
        )
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        self.assertEqual(completed["bootstrap_fact_id"], "f001")
        self.assertEqual(completed["completion_intent_id"], "i002")
        self.assertTrue(completed["bootstrap_attempted"])
        self.assertEqual(completed["bootstrap_result"], "completed")
        self.assertTrue(audit["ok"])
        self.assertTrue(audit["completion"]["valid"])
        self.assertTrue(audit["checkpoint"]["bootstrap_attempted"])
        self.assertEqual(audit["checkpoint"]["bootstrap_result"], "completed")
        self.assertEqual(audit["completion"]["supporting_fact_ids"], ["f001"])
        facts = {fact["id"]: fact["description"] for fact in graph["facts"]}
        self.assertEqual(facts["f001"], "direct evidence")
        completion_intent = [intent for intent in graph["intents"] if intent["to_fact_id"] == "goal"][0]
        self.assertEqual(completion_intent["sources"], ["f001"])

    def test_bootstrap_noop_is_durable(self) -> None:
        run = self.init_run()
        before = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        graph_before = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        noop = self.load_json(
            self.run_cli(
                run,
                "bootstrap-noop",
                "--run",
                str(run),
                "--worker",
                "bootstrap-worker",
                "--stdin",
                stdin={"reason": "direct completion is not verifiable from current context"},
            )
        )
        after = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        graph_after = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        self.assertFalse(before["bootstrap_attempted"])
        self.assertTrue(noop["bootstrap_attempted"])
        self.assertEqual(noop["bootstrap_result"], "noop")
        self.assertTrue(after["bootstrap_attempted"])
        self.assertEqual(after["bootstrap_result"], "noop")
        self.assertEqual(after["bootstrap_worker"], "bootstrap-worker")
        self.assertGreater(after["last_event_seq"], before["last_event_seq"])
        self.assertEqual(len(graph_before["facts"]), len(graph_after["facts"]))
        self.assertEqual(len(graph_before["intents"]), len(graph_after["intents"]))
        with self.db(run) as conn:
            event_count = conn.execute("SELECT COUNT(*) AS count FROM events WHERE type = 'bootstrap_noop'").fetchone()["count"]
        self.assertEqual(event_count, 1)

    def test_bootstrap_noop_is_idempotent(self) -> None:
        run = self.init_run()
        stdin = {"reason": "direct completion is not verifiable from current context"}
        first = self.load_json(
            self.run_cli(
                run,
                "bootstrap-noop",
                "--run",
                str(run),
                "--worker",
                "bootstrap-worker",
                "--stdin",
                stdin=stdin,
            )
        )
        event_seq = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))["last_event_seq"]
        second = self.load_json(
            self.run_cli(
                run,
                "bootstrap-noop",
                "--run",
                str(run),
                "--worker",
                "bootstrap-worker",
                "--stdin",
                stdin=stdin,
            )
        )
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertEqual(first, second)
        self.assertEqual(checkpoint["last_event_seq"], event_seq)
        with self.db(run) as conn:
            event_count = conn.execute("SELECT COUNT(*) AS count FROM events WHERE type = 'bootstrap_noop'").fetchone()["count"]
        self.assertEqual(event_count, 1)

    def test_bootstrap_complete_after_noop_rejected(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "bootstrap-noop",
            "--run",
            str(run),
            "--worker",
            "bootstrap-worker",
            "--stdin",
            stdin={"reason": "direct completion is not verifiable from current context"},
        )
        result = self.run_cli(
            run,
            "bootstrap-complete",
            "--run",
            str(run),
            "--worker",
            "bootstrap-worker",
            "--stdin",
            stdin={
                "fact": {"description": "direct evidence"},
                "complete": {"description": "goal satisfied directly"},
            },
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed after bootstrap-noop", result.stderr)

    def test_bootstrap_noop_rejects_disabled_bootstrap(self) -> None:
        run = self.init_run(no_bootstrap=True)
        result = self.run_cli(
            run,
            "bootstrap-noop",
            "--run",
            str(run),
            "--worker",
            "bootstrap-worker",
            "--stdin",
            stdin={"reason": "disabled"},
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_reason_worker_flow_can_create_intents(self) -> None:
        run = self.init_run()
        before = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.run_cli(run, "reason-claim", "--run", str(run), "--worker", "reason-worker", "--trigger", "checkpoint")
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason-worker",
            "--stdin",
            stdin={"from": ["origin"], "description": "explore missing evidence"},
        )
        self.run_cli(run, "reason-release", "--run", str(run), "--worker", "reason-worker")
        after = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertEqual(after["open_intent_count"], 1)
        self.assertGreater(after["last_event_seq"], before["last_event_seq"])

    def test_explore_self_claim_conclude_flow(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason-worker",
            "--stdin",
            stdin={"from": ["origin"], "description": "explore"},
        )
        claimed = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "explore-worker", "--json"))
        self.run_cli(run, "intent-heartbeat", "--run", str(run), "--intent", claimed["intent_id"], "--worker", "explore-worker")
        concluded = self.load_json(
            self.run_cli(
                run,
                "intent-conclude",
                "--run",
                str(run),
                "--intent",
                claimed["intent_id"],
                "--worker",
                "explore-worker",
                "--stdin",
                stdin={"description": "new evidence"},
            )
        )
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        self.assertEqual(concluded["to_fact_id"], "f001")
        self.assertEqual(checkpoint["open_intent_count"], 0)
        intent = [item for item in graph["intents"] if item["id"] == claimed["intent_id"]][0]
        self.assertEqual(intent["to_fact_id"], "f001")
        self.assertIsNotNone(intent["concluded_at"])

    def test_expired_intent_lease_clears_lazily(self) -> None:
        run = self.init_run(intent_timeout=1)
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"from": ["origin"], "description": "explore"},
        )
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        with self.db(run) as conn:
            conn.execute("UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = 'i001'")
            conn.commit()
        claim = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w2", "--intent", "i001"))
        self.assertEqual(claim["worker"], "w2")

    def test_reason_lease_conflicts_and_expires(self) -> None:
        run = self.init_run(reason_timeout=300)
        self.run_cli(run, "reason-claim", "--run", str(run), "--worker", "r1", "--trigger", "graph")
        conflict = self.run_cli(run, "reason-claim", "--run", str(run), "--worker", "r2", "--trigger", "graph", check=False)
        self.assertEqual(conflict.returncode, 3)
        with self.db(run) as conn:
            conn.execute(
                """
                UPDATE projects
                SET reason_started_at = '2000-01-01T00:00:00Z',
                    reason_last_heartbeat_at = '2000-01-01T00:00:00Z'
                WHERE id = 'p001'
                """
            )
            conn.execute("UPDATE settings SET reason_timeout = 1")
            conn.commit()
        claim = self.load_json(self.run_cli(run, "reason-claim", "--run", str(run), "--worker", "r2", "--trigger", "graph"))
        self.assertEqual(claim["worker"], "r2")

    def test_conclude_complete_and_audit(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"accepted": True, "data": {"from": ["origin"], "description": "explore"}},
        )
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        concluded = self.load_json(
            self.run_cli(
                run,
                "intent-conclude",
                "--run",
                str(run),
                "--intent",
                "i001",
                "--worker",
                "w1",
                "--stdin",
                stdin={"accepted": True, "data": {"description": "implemented feature"}},
            )
        )
        completed = self.load_json(
            self.run_cli(
                run,
                "complete",
                "--run",
                str(run),
                "--worker",
                "reason",
                "--stdin",
                stdin={"from": [concluded["to_fact_id"]], "description": "goal satisfied"},
            )
        )
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        self.assertEqual(completed["completion_intent_id"], "i002")
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["project"]["status"], "completed")
        self.assertEqual(audit["completion"]["goal_fact_id"], "goal")
        self.assertEqual(audit["completion"]["supporting_fact_ids"], ["f001"])

    def test_reopen_creates_feedback_fact_and_external_feedback_intent(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"from": ["origin"], "description": "explore"},
        )
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i001",
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "fact"},
        )
        self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason",
            "--stdin",
            stdin={"from": ["f001"], "description": "done"},
        )
        reopened = self.load_json(
            self.run_cli(
                run,
                "reopen",
                "--run",
                str(run),
                "--creator",
                "user",
                "--stdin",
                stdin={"description": "needs change"},
            )
        )
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        self.assertEqual(audit["project"]["status"], "active")
        self.assertFalse(audit["completion"]["present"])
        self.assertEqual(reopened["feedback_fact_id"], "f002")
        feedback_intent = [intent for intent in graph["intents"] if intent["id"] == reopened["external_feedback_intent_id"]][0]
        self.assertEqual(feedback_intent["to_fact_id"], "f002")
        self.assertEqual(feedback_intent["sources"], ["f001"])

    def test_hint_add_allowed_after_completed(self) -> None:
        run = self.init_run()
        self.run_cli(
            run,
            "bootstrap-complete",
            "--run",
            str(run),
            "--worker",
            "bootstrap-worker",
            "--stdin",
            stdin={
                "fact": {"description": "direct evidence"},
                "complete": {"description": "goal satisfied directly"},
            },
        )
        checkpoint_before = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        hint = self.load_json(
            self.run_cli(
                run,
                "hint-add",
                "--run",
                str(run),
                "--creator",
                "user",
                "--stdin",
                stdin={"content": "post-completion note"},
            )
        )
        checkpoint_after = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertEqual(hint["hint_id"], "h001")
        self.assertEqual(checkpoint_after["hint_count"], checkpoint_before["hint_count"] + 1)

    def test_hook_events_separate_from_mutation_events(self) -> None:
        run = self.init_hook_run()
        result = self.run_hook("stop", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WAYMARK_GRAPH_CHECKPOINT", result.stdout)
        self.assertTrue((run / "hook-events.jsonl").exists())
        self.assertIn("hook.stop", (run / "hook-events.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("hook.stop", (run / "events.jsonl").read_text(encoding="utf-8"))
        with self.db(run) as conn:
            event_count = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        self.assertGreater(event_count, 0)

    def test_main_does_not_preclaim_explore_protocol(self) -> None:
        protocol = (ROOT / "templates" / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertNotIn("Claim open intents newest first", protocol)
        self.assertNotIn("intent-claim --run", protocol)
        self.assertIn("The explore worker claims one open intent itself", protocol)

    def test_protocol_uses_checkpoint_bootstrap_attempted(self) -> None:
        protocol = (ROOT / "templates" / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("WAYMARK_RUN_COMPLETE", protocol)
        self.assertIn("checkpoint.bootstrap_attempted=false", protocol)
        self.assertNotIn("bootstrap has not run yet", protocol)

    def test_exports_are_ordered_and_available(self) -> None:
        run = self.init_run()
        yaml_text = self.run_cli(run, "export", "--run", str(run), "--format", "yaml").stdout
        timeline = self.run_cli(run, "export", "--run", str(run), "--format", "timeline").stdout
        markdown = self.run_cli(run, "export", "--run", str(run), "--format", "markdown").stdout
        self.assertIn("project:", yaml_text)
        self.assertIn("FACT origin", timeline)
        self.assertIn("FACT goal", timeline)
        self.assertIn("# Test", markdown)

    def test_validator_rejects_invalid_shapes(self) -> None:
        run = self.init_run()
        result = self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason",
            "--stdin",
            stdin={"accepted": True, "data": {"from": ["origin"], "description": ""}},
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_intent_claim_is_fifo(self) -> None:
        run = self.init_run()
        self.create_intent(run, "oldest")
        self.create_intent(run, "newest")
        claimed = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1"))
        self.assertEqual(claimed["intent_id"], "i001")

    def test_intent_priority_overrides_fifo(self) -> None:
        run = self.init_run()
        self.create_intent(run, "default priority")
        self.create_intent(run, "urgent", priority=-1)
        claimed = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1"))
        self.assertEqual(claimed["intent_id"], "i002")
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i002",
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "urgent done"},
        )
        claimed = self.load_json(self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1"))
        self.assertEqual(claimed["intent_id"], "i001")

    def test_intent_create_enforces_open_intent_cap(self) -> None:
        run = self.init_run(max_intents=2)
        self.create_intent(run, "one")
        self.create_intent(run, "two")
        result = self.run_cli(
            run,
            "intent-create",
            "--run",
            str(run),
            "--creator",
            "reason-worker",
            "--stdin",
            stdin={"from": ["origin"], "description": "three"},
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("open intent cap reached", result.stderr)

    def test_three_releases_abandon_intent(self) -> None:
        run = self.init_run()
        self.create_intent(run, "fragile")
        for attempt in range(3):
            self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
            released = self.load_json(
                self.run_cli(
                    run,
                    "intent-release",
                    "--run",
                    str(run),
                    "--intent",
                    "i001",
                    "--worker",
                    "w1",
                    "--reason",
                    f"failed attempt {attempt + 1}",
                )
            )
        self.assertEqual(released["release_count"], 3)
        self.assertTrue(released["abandoned"])
        blocked = self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1", check=False)
        self.assertEqual(blocked.returncode, 1)
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertEqual(checkpoint["open_intent_count"], 0)
        self.assertEqual(checkpoint["abandoned_intent_count"], 1)
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        intent = [item for item in graph["intents"] if item["id"] == "i001"][0]
        self.assertIsNotNone(intent["abandoned_at"])
        self.assertEqual(intent["last_release_reason"], "failed attempt 3")
        self.assertEqual(intent["attempt_count"], 3)

    def test_round_start_tracks_stall_and_handoff(self) -> None:
        run = self.init_run(max_rounds=2)
        first = self.load_json(self.run_cli(run, "round-start", "--run", str(run), "--json"))
        self.assertEqual(first["round_count"], 1)
        self.assertEqual(first["rounds_without_progress"], 0)
        self.assertFalse(first["should_handoff"])
        second = self.load_json(self.run_cli(run, "round-start", "--run", str(run), "--json"))
        self.assertEqual(second["rounds_without_progress"], 1)
        self.assertFalse(second["should_handoff"])
        third = self.load_json(self.run_cli(run, "round-start", "--run", str(run), "--json"))
        self.assertEqual(third["rounds_without_progress"], 2)
        self.assertTrue(third["should_handoff"])
        self.run_cli(
            run, "hint-add", "--run", str(run), "--creator", "user", "--stdin", stdin={"content": "new info"}
        )
        fourth = self.load_json(self.run_cli(run, "round-start", "--run", str(run), "--json"))
        self.assertEqual(fourth["rounds_without_progress"], 0)
        self.assertFalse(fourth["should_handoff"])

    def test_heartbeats_do_not_count_as_round_progress(self) -> None:
        run = self.init_run(max_rounds=5)
        self.create_intent(run)
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        self.run_cli(run, "round-start", "--run", str(run), "--json")
        self.run_cli(run, "intent-heartbeat", "--run", str(run), "--intent", "i001", "--worker", "w1")
        second = self.load_json(self.run_cli(run, "round-start", "--run", str(run), "--json"))
        self.assertEqual(second["rounds_without_progress"], 1)

    def test_should_reason_retriggers_only_on_new_information(self) -> None:
        run = self.init_run()
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertTrue(checkpoint["should_reason"])
        self.run_cli(run, "reason-claim", "--run", str(run), "--worker", "r1", "--trigger", "checkpoint")
        self.create_intent(run, "reason's own intent")
        self.run_cli(run, "reason-release", "--run", str(run), "--worker", "r1")
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertFalse(checkpoint["should_reason"])
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i001",
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "new evidence"},
        )
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertTrue(checkpoint["should_reason"])

    def test_complete_requires_full_criteria_mapping(self) -> None:
        run = self.init_run(criteria=["tests pass", "docs updated"])
        self.create_intent(run)
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i001",
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "evidence"},
        )
        unmapped = self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason-worker",
            "--stdin",
            stdin={"from": ["f001"], "description": "done"},
            check=False,
        )
        self.assertEqual(unmapped.returncode, 2)
        self.assertIn("unmapped: c001, c002", unmapped.stderr)
        partial = self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason-worker",
            "--stdin",
            stdin={"from": ["f001"], "description": "done", "criteria": {"c001": ["f001"]}},
            check=False,
        )
        self.assertEqual(partial.returncode, 2)
        completed = self.load_json(
            self.run_cli(
                run,
                "complete",
                "--run",
                str(run),
                "--worker",
                "reason-worker",
                "--stdin",
                stdin={
                    "from": ["f001"],
                    "description": "done",
                    "criteria": {"c001": ["f001"], "c002": "f001"},
                },
            )
        )
        self.assertEqual(completed["criteria"], {"c001": ["f001"], "c002": ["f001"]})
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        self.assertTrue(audit["ok"])
        self.assertTrue(all(criterion["satisfied"] for criterion in audit["criteria"]))

    def test_bootstrap_complete_auto_maps_criteria(self) -> None:
        run = self.init_run(criteria=["direct evidence recorded"])
        self.run_cli(
            run,
            "bootstrap-complete",
            "--run",
            str(run),
            "--worker",
            "bootstrap-worker",
            "--stdin",
            stdin={
                "fact": {"description": "direct evidence"},
                "complete": {"description": "goal satisfied directly"},
            },
        )
        audit = self.load_json(self.run_cli(run, "audit", "--run", str(run), "--json"))
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["criteria"][0]["fact_ids"], ["f001"])
        self.assertTrue(audit["criteria"][0]["satisfied"])

    def test_verify_reports_evidence_coverage(self) -> None:
        run = self.init_run(criteria=["artifact exists"])
        evidence = self.tmp_path / "evidence.txt"
        evidence.write_text("proof\n", encoding="utf-8")
        self.create_intent(run)
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i001",
            "--worker",
            "w1",
            "--stdin",
            stdin={
                "description": "artifact produced",
                "evidence_path": str(evidence),
                "evidence_cmd": "cat evidence.txt",
            },
        )
        self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason-worker",
            "--stdin",
            stdin={"from": ["f001"], "description": "done", "criteria": {"c001": ["f001"]}},
        )
        verify = self.load_json(self.run_cli(run, "verify", "--run", str(run), "--json"))
        self.assertTrue(verify["audit_ok"])
        self.assertEqual(verify["coverage"]["facts_total"], 1)
        self.assertEqual(verify["coverage"]["re_verifiable"], 1)
        self.assertEqual(verify["coverage"]["trust_prior"], 0)
        self.assertEqual(verify["coverage"]["paths_ok"], 1)
        self.assertEqual(verify["coverage"]["paths_missing"], 0)
        self.assertTrue(verify["supporting_facts"][0]["evidence_path_exists"])
        evidence.unlink()
        verify = self.load_json(self.run_cli(run, "verify", "--run", str(run), "--json"))
        self.assertEqual(verify["coverage"]["paths_missing"], 1)
        self.assertFalse(verify["supporting_facts"][0]["evidence_path_exists"])

    def test_context_returns_scoped_intent_view(self) -> None:
        run = self.init_run()
        self.create_intent(run, "scoped work")
        context = self.load_json(self.run_cli(run, "context", "--run", str(run), "--intent", "i001", "--json"))
        self.assertEqual(context["intent"]["id"], "i001")
        self.assertEqual([fact["id"] for fact in context["source_facts"]], ["origin"])
        self.assertEqual(context["goal"]["id"], "goal")
        self.assertNotIn("facts", context)
        unknown = self.run_cli(run, "context", "--run", str(run), "--intent", "i999", check=False)
        self.assertEqual(unknown.returncode, 2)

    def test_brief_returns_scoped_reason_view(self) -> None:
        run = self.init_run(criteria=["check one"])
        self.create_intent(run, "open work")
        brief = self.load_json(self.run_cli(run, "brief", "--run", str(run), "--json"))
        self.assertEqual(brief["goal"]["id"], "goal")
        self.assertEqual([intent["id"] for intent in brief["open_intents"]], ["i001"])
        self.assertEqual(brief["abandoned_intents"], [])
        self.assertEqual(brief["criteria"][0]["id"], "c001")
        self.assertEqual({fact["id"] for fact in brief["fact_index"]}, {"origin", "goal"})
        self.assertIn("checkpoint", brief)

    def test_heartbeat_skips_view_regeneration(self) -> None:
        run = self.init_run()
        self.create_intent(run)
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        (run / "graph.yaml").unlink()
        self.run_cli(run, "intent-heartbeat", "--run", str(run), "--intent", "i001", "--worker", "w1")
        self.assertFalse((run / "graph.yaml").exists())
        self.run_cli(run, "checkpoint", "--run", str(run), "--json")
        self.assertTrue((run / "graph.yaml").exists())

    def test_fact_description_cap(self) -> None:
        run = self.init_run()
        self.create_intent(run)
        self.run_cli(run, "intent-claim", "--run", str(run), "--worker", "w1")
        result = self.run_cli(
            run,
            "intent-conclude",
            "--run",
            str(run),
            "--intent",
            "i001",
            "--worker",
            "w1",
            "--stdin",
            stdin={"description": "x" * 1300},
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("evidence_path", result.stderr)

    def test_init_records_criteria_and_objective(self) -> None:
        run = self.init_run(criteria=["tests pass", "docs updated"])
        graph = self.load_json(self.run_cli(run, "graph", "--run", str(run), "--format", "json"))
        self.assertEqual([criterion["id"] for criterion in graph["criteria"]], ["c001", "c002"])
        objective = (run / "OBJECTIVE.md").read_text(encoding="utf-8")
        self.assertIn("## Acceptance Criteria", objective)
        self.assertIn("`c001` tests pass", objective)
        checkpoint = self.load_json(self.run_cli(run, "checkpoint", "--run", str(run), "--json"))
        self.assertEqual(checkpoint["criteria_count"], 2)

    def test_init_captures_git_baseline_ref(self) -> None:
        import shutil

        if shutil.which("git") is None:
            self.skipTest("git is not available")
        repo = self.tmp_path / "repo"
        repo.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=env, capture_output=True, text=True
        ).stdout.strip()
        run = repo / ".waymark" / "run"
        result = subprocess.run(
            [str(CLI), "init", "--run", str(run), "--title", "T", "--origin", "o", "--goal", "g"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["baseline_ref"], head)
        verify = self.load_json(self.run_cli(run, "verify", "--run", str(run), "--json"))
        self.assertEqual(verify["baseline_ref"], head)

    def test_schema_drops_counters_and_keeps_single_settings_row(self) -> None:
        run = self.init_run()
        with self.db(run) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("counters", tables)
            self.assertIn("criteria", tables)
            settings_rows = conn.execute("SELECT id, max_rounds FROM settings").fetchall()
            indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertEqual(len(settings_rows), 1)
        self.assertEqual(settings_rows[0]["id"], 1)
        self.assertIn("idx_intents_open", indexes)

    def test_hook_skips_waymark_cli_calls(self) -> None:
        run = self.init_hook_run()
        hook_log = run / "hook-events.jsonl"

        def post_tool_use(payload: dict) -> None:
            result = self.run_hook("post-tool-use", payload)
            self.assertEqual(result.returncode, 0, result.stderr)

        post_tool_use({"tool_name": "Bash", "tool_input": {"command": f"{CLI} checkpoint --run x --json"}})
        self.assertFalse(hook_log.exists())
        post_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        self.assertTrue(hook_log.exists())
        self.assertEqual(len(hook_log.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_strict_stop_blocks_incomplete_run(self) -> None:
        run = self.init_hook_run()
        result = self.run_hook("stop", {}, strict=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(str(run), result.stderr)
        self.assertIn("final-status.status: not_completed", result.stderr)
        self.assertIn("protocol loop", result.stderr)
        # The gate stays read-only: the stop observation is still logged, and
        # nothing lands in the mutation event log.
        self.assertIn("hook.stop", (run / "hook-events.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("hook.stop", (run / "events.jsonl").read_text(encoding="utf-8"))

    def test_default_stop_stays_observational_on_incomplete_run(self) -> None:
        self.init_hook_run()
        result = self.run_hook("stop", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WAYMARK_GRAPH_CHECKPOINT", result.stdout)

    def test_strict_stop_skips_when_stop_hook_active(self) -> None:
        self.init_hook_run()
        result = self.run_hook("stop", {"stop_hook_active": True}, strict=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_mode_leaves_other_events_observational(self) -> None:
        self.init_hook_run()
        for event in ("post-tool-use", "subagent-stop"):
            result = self.run_hook(event, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, strict=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_stop_blocks_until_verification_recorded(self) -> None:
        run = self.init_hook_run()
        self.complete_run(run)
        result = self.run_hook("stop", {}, strict=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("final-status.status: verification_missing", result.stderr)
        self.assertIn("verification-record", result.stderr)

    def test_strict_stop_blocks_on_failed_verification(self) -> None:
        run = self.init_hook_run()
        self.complete_run(run)
        self.record_verification(run, {"verified": False, "reason": "criterion unmet"})
        result = self.run_hook("stop", {}, strict=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("final-status.status: verification_failed", result.stderr)
        self.assertIn("criterion unmet", result.stderr)

    def test_strict_stop_allows_ready_run(self) -> None:
        run = self.init_hook_run()
        self.complete_run(run)
        self.record_verification(run, {"verified": True, "evidence": "checked"})
        self.assertTrue(self.final_status(run)["ready"])
        result = self.run_hook("stop", {}, strict=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WAYMARK_GRAPH_CHECKPOINT", result.stdout)

    def test_strict_stop_allows_handoff(self) -> None:
        run = self.init_hook_run(max_rounds=1)
        self.run_cli(run, "round-start", "--run", str(run), "--json")
        self.run_cli(run, "round-start", "--run", str(run), "--json")
        self.assertEqual(self.final_status(run)["status"], "handoff")
        result = self.run_hook("stop", {}, strict=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_readme_documents_strict_hooks(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("WAYMARK_STRICT_HOOKS=1", readme)
        self.assertIn("observational by default", readme)
        self.assertIn("read-only completion gate", readme)

    def test_protocol_uses_round_start_and_verifier(self) -> None:
        protocol = (ROOT / "templates" / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("round-start", protocol)
        self.assertIn("should_handoff", protocol)
        self.assertIn("should_reason", protocol)
        self.assertIn("verifier-worker", protocol)
        self.assertIn("WAYMARK_VERIFICATION", protocol)
        self.assertNotIn("configured number of rounds", protocol)

    def test_skill_review_gate_precedes_init(self) -> None:
        skill = (ROOT / "skills" / "waymark-goal" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Review gate", skill)
        self.assertIn("never assume confirmation", skill)
        self.assertIn("Never run `waymark init` before the review gate resolves", skill)
        self.assertLess(skill.index("Review gate"), skill.index("waymark init --run"))

    def test_skill_seeds_mission_contract_with_two_semantic_gates(self) -> None:
        skill = (ROOT / "skills" / "waymark-goal" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("mission contract", skill)
        # The two human gates are semantic; the /goal paste is host mechanics.
        self.assertIn(
            "Two semantic human gates only: clarification for true gaps, then mission contract review before init",
            skill,
        )
        self.assertIn("dispatch step, not an extra planning gate", skill)
        self.assertIn("runs autonomously through `PROTOCOL.md`", skill)
        self.assertIn("`final-status.ready=true` or handoff", skill)
        # The contract carries more than goal and criteria.
        for field in ("constraints", "assumptions", "handoff triggers"):
            self.assertIn(field, skill)
        # Clarification is bounded by abstract gap categories that hold in any
        # domain, never a software-only intake checklist.
        for category in (
            "desired end state",
            "scope boundaries",
            "acceptance evidence",
            "priority tradeoffs",
            "risk tolerance",
            "handoff or stop conditions",
        ):
            self.assertIn(category, skill)
        for software_default in ("framework", "test command", "API compatibility"):
            self.assertNotIn(software_default, skill)

    def test_final_status_on_active_project(self) -> None:
        run = self.init_run(criteria=["check one"])
        status = self.final_status(run)
        self.assertFalse(status["ready"])
        self.assertEqual(status["status"], "not_completed")
        self.assertFalse(status["audit_ok"])
        self.assertFalse(status["verification_ok"])
        self.assertFalse(status["criteria_ok"])
        self.assertFalse(status["should_handoff"])
        self.assertIsNone(status["completion_intent_id"])
        self.assertIsNone(status["verification"])

    def test_final_status_reports_handoff_when_stalled(self) -> None:
        run = self.init_run(max_rounds=1)
        self.run_cli(run, "round-start", "--run", str(run), "--json")
        self.run_cli(run, "round-start", "--run", str(run), "--json")
        status = self.final_status(run)
        self.assertEqual(status["status"], "handoff")
        self.assertTrue(status["should_handoff"])
        self.assertFalse(status["ready"])

    def test_final_status_requires_verification_after_completion(self) -> None:
        run = self.init_run()
        self.complete_run(run)
        status = self.final_status(run)
        self.assertFalse(status["ready"])
        self.assertEqual(status["status"], "verification_missing")
        self.assertTrue(status["audit_ok"])
        self.assertFalse(status["verification_ok"])
        self.assertEqual(status["completion_intent_id"], "i002")
        self.assertTrue(any("verification record is missing" in error for error in status["errors"]))

    def test_verification_record_persists_and_unlocks_ready(self) -> None:
        run = self.init_run()
        self.complete_run(run)
        result = self.record_verification(
            run, {"verified": True, "evidence": "re-ran commands", "re_verified": 3, "trust_prior": 1}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"verification_id": "v001", "verified": True})
        with self.db(run) as conn:
            row = conn.execute("SELECT * FROM verification_runs WHERE id = 'v001'").fetchone()
        self.assertEqual(row["worker"], "verifier-worker")
        self.assertEqual(row["verified"], 1)
        self.assertEqual(row["re_verified"], 3)
        self.assertEqual(row["trust_prior"], 1)
        status = self.final_status(run)
        self.assertTrue(status["ready"])
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["verification_ok"])
        self.assertEqual(status["verification"]["id"], "v001")
        self.assertEqual(status["errors"], [])

    def test_failed_verification_blocks_readiness(self) -> None:
        run = self.init_run()
        self.complete_run(run)
        # Failed verdicts arrive in the worker's rejected shape and must still persist.
        result = self.record_verification(
            run, {"accepted": False, "data": {"verified": False, "reason": "evidence file empty"}}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.final_status(run)
        self.assertFalse(status["ready"])
        self.assertEqual(status["status"], "verification_failed")
        self.assertTrue(any("evidence file empty" in error for error in status["errors"]))
        # A newer passing record supersedes the failure.
        self.record_verification(run, {"verified": True, "evidence": "fixed and re-checked"})
        status = self.final_status(run)
        self.assertTrue(status["ready"])
        self.assertEqual(status["verification"]["id"], "v002")

    def test_verification_record_rejected_on_active_project(self) -> None:
        run = self.init_run()
        result = self.record_verification(run, {"verified": True})
        self.assertEqual(result.returncode, 2)
        self.assertIn("completed project", result.stderr)
        bad_payload = self.record_verification(run, {"verified": "yes"})
        self.assertEqual(bad_payload.returncode, 2)

    def test_reopen_invalidates_verification(self) -> None:
        run = self.init_run()
        self.complete_run(run)
        self.record_verification(run, {"verified": True, "evidence": "checked"})
        self.assertTrue(self.final_status(run)["ready"])
        self.run_cli(
            run, "reopen", "--run", str(run), "--creator", "user", "--stdin", stdin={"description": "wrong result"}
        )
        status = self.final_status(run)
        self.assertFalse(status["ready"])
        self.assertIsNone(status["verification"])
        self.run_cli(
            run,
            "complete",
            "--run",
            str(run),
            "--worker",
            "reason-worker",
            "--stdin",
            stdin={"from": ["f001"], "description": "done again"},
        )
        status = self.final_status(run)
        self.assertEqual(status["status"], "verification_missing")
        self.assertFalse(status["ready"])

    def test_workflow_docs_mark_dynamic_workflow_optional(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Dynamic Workflow", readme)
        self.assertIn("not the default", readme)
        self.assertIn("final-status", readme)
        self.assertIn("not yet a fully installed native runtime", readme)
        self.assertIn("DYNAMIC_WORKFLOW_REFERENCE.js", readme)
        self.assertIn("reference dispatcher", readme)
        self.assertIn("transaction and recovery layer", readme)
        guide = (ROOT / "templates" / "WORKFLOW_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("optional dispatcher", guide)
        self.assertIn("min(open_intent_count, 4)", guide)
        self.assertIn("one workflow round equals one completed agent wave", guide)
        self.assertIn("final-status", guide)
        self.assertIn("DYNAMIC_WORKFLOW_REFERENCE.js", guide)
        self.assertIn("illustrative, not authoritative", guide)
        self.assertIn("not required for normal `/goal` use", guide)
        protocol = (ROOT / "templates" / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("final-status", protocol)
        self.assertIn("verification-record", protocol)
        verifier = (ROOT / "agents" / "verifier-worker.md").read_text(encoding="utf-8")
        self.assertIn("verification-record", verifier)

    def test_workflow_reference_is_dispatcher_gated_on_final_status(self) -> None:
        reference = (ROOT / "templates" / "DYNAMIC_WORKFLOW_REFERENCE.js").read_text(encoding="utf-8")
        self.assertIn("export const meta", reference)
        self.assertIn("dispatcher only", reference)
        # Positioned as illustrative spec material, never an installed runtime.
        self.assertIn("REFERENCE, not an installed runtime", reference)
        self.assertIn("not required for normal", reference)
        self.assertIn("must not own state", reference)
        self.assertIn("MAX_PARALLEL_EXPLORE = 4", reference)
        self.assertIn("not an architectural limit", reference)
        self.assertIn("args.maxParallelExplore || 4", reference)
        self.assertIn("Math.min(cp.open_intent_count, MAX_PARALLEL_EXPLORE)", reference)
        for command in ("round-start", "checkpoint", "audit", "final-status"):
            self.assertIn(f"cli('{command}'", reference)
        for worker in ("bootstrap-worker", "reason-worker", "explore-worker", "verifier-worker"):
            self.assertIn(f"'{worker}'", reference)
        self.assertIn("if (final.ready)", reference)
        self.assertIn("verification-record", reference)
        # Dispatcher never touches the database file or trusts prose: every
        # state change goes through workers running bin/waymark.
        self.assertNotIn("blackboard.sqlite", reference)
        self.assertIn("never mutates SQLite directly", reference)
        self.assertIn("bin/waymark", reference)
        self.assertIn("structured output", reference)


if __name__ == "__main__":
    unittest.main()
