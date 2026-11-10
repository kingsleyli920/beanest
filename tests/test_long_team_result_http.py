"""Long native task prose crosses real HTTP/SQLite; the model is synthetic."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError, checked_text
from local_agent_coordinator.message_capacity import MAX_MESSAGE_UTF8_BYTES
from local_agent_coordinator.room_service import _response
import test_native_room_service as native
import test_team_runs_http as http
from test_team_store import proposal
import test_team_store as store_fixtures
from wait_support import scaled


class LongTeamResultHTTPTests(unittest.TestCase):
    request = http.TeamRunHTTPTests.request

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "notes.md").write_text("Unchanged source.\n")
        self.native = native.NativeTransport()
        self.native.handler = self.handler
        self.result = "完整核查结果与证据边界。" * 800
        self.reviewed = []
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   native_invoke=self.native, registry=default_registry())
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        code, value = self.request("POST", "/api/projects", {
            "name": "Long report example", "path": str(self.source), "selection_kind": "folder"})
        self.assertEqual(code, 200, value)
        self.project_id = value["project"]["id"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temporary.cleanup()

    def handler(self, agent, context, cwd):
        phase = context["job"]["job_phase"]
        commands = []
        if phase in {"plan", "coordinate"}:
            if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                commands = [proposal("K1"), {"type": "claim", "task_id": "K1"}]
            commands.append({"type": "plan_ready"})
        elif phase == "work":
            commands = [{"type": "complete", "task_id": "K1", "result": self.result}]
        elif phase == "review":
            result = context["task"]["result"]
            self.reviewed.append(context["full_texts"][result["sha256"]]["text"] if isinstance(result, dict) else result)
            commands = [{"type": "review", "task_id": "K1", "verdict": "accept", "reason": "Read the full report."}]
        elif phase == "checkpoint":
            commands = [{"type": "checkpoint", "decision": "complete", "summary": "The report is reviewed.",
                         "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}]
        elif phase == "checkpoint_review":
            commands = [{"type": "checkpoint_review", "verdict": "accept", "reason": "The requested report exists.",
                         "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}]
        else:
            self.fail(phase)
        return native.native_reply(context, commands)

    def create(self):
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Produce and independently review the full factual report.", "target": "claude", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        return created

    def settled(self, room_id, *, stall=5, limit=60):
        """Wait until no job is queued or working and no worker is alive.

        A room with two dozen synthetic calls can need more than `stall`
        seconds in total on a loaded CI runner while it changes state every
        fraction of a second. Fail when nothing changes for `stall` seconds (a
        stuck room) or after `limit` seconds in total (a room that never ends).
        """
        stall, limit = scaled(stall), scaled(limit)
        start = changed = time.monotonic()
        seen = None
        while True:
            code, value = self.request("GET", "/api/rooms/" + room_id)
            self.assertEqual(code, 200, value)
            if (not any(job["status"] in {"queued", "working"} for job in value["jobs"])
                    and not any(room == room_id and not worker.done() for (room, _), worker in list(self.service._workers.items()))):
                return value
            now = time.monotonic()
            progress = (value["events"][-1]["seq"] if value["events"] else None,
                        [(job["id"], job["status"]) for job in value["jobs"]])
            if progress != seen:
                seen, changed = progress, now
            if now - changed > stall or now - start > limit:
                self.fail(f"Synthetic native room did not settle: unchanged for {now - changed:.1f}s, "
                          f"{now - start:.1f}s in total; jobs {[(job['provider'], job['status'], job['error']) for job in value['jobs']]}")
            time.sleep(0.005)

    def test_long_result_is_reviewed_persisted_and_lazy_read_without_truncation(self):
        created = self.create()
        room_id = created["room"]["id"]
        final = self.settled(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        task = final["team"]["tasks"][0]
        self.assertEqual(task["result"], self.result)
        self.assertEqual(self.reviewed, [self.result])
        self.assertEqual(task["status"], "accepted")
        code, state = self.request("GET", f"/api/rooms/{room_id}/state")
        self.assertEqual(code, 200, state)
        reference = state["team"]["tasks"][0]["result_text_ref"]
        route = f"/api/rooms/{room_id}/text?" + urlencode({key: value for key, value in reference.items() if key != "room_id"})
        code, exact = self.request("GET", route)
        self.assertEqual(code, 200, exact)
        self.assertEqual(exact["text"], self.result)
        self.assertEqual(exact["sha256"], hashlib.sha256(self.result.encode()).hexdigest())
        for changed in ({**reference, "sha256": "0" * 64}, {**reference, "result_version": task["result_version"] + 1}):
            route = f"/api/rooms/{room_id}/text?" + urlencode({key: value for key, value in changed.items() if key != "room_id"})
            self.assertEqual(self.request("GET", route)[0], 409)
        self.assertFalse(any(job["error"] for job in final["jobs"]))
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")

    def legacy_failure_and_carry(self):
        """Use only public HTTP; the temporary host alone emulates two old gates."""
        self.phases = []
        normal = self.handler

        async def three_tasks(agent, context, cwd):
            phase, task = context["job"]["job_phase"], context.get("task")
            self.phases.append((phase, task["id"] if task else None, context["job"]["assignment_epoch"]))
            if phase in {"plan", "coordinate"}:
                commands = []
                if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                    commands = [proposal("K1"), proposal("K2"), proposal("K3", dependencies=["K1", "K2"]),
                                {"type": "claim", "task_id": "K1"}]
                elif agent == "codex" and context["team"]["tasks"]:
                    commands = [{"type": "claim", "task_id": key} for key in ("K2", "K3")]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if phase == "work":
                if task["id"] == "K2":
                    for _ in range(200):
                        room_id = self.service.store.list_rooms()["rooms"][0]["id"]
                        board = self.service.store.snapshot(room_id)["team"]["tasks"]
                        if next(item for item in board if item["id"] == "K1")["result_version"] == 1:
                            break
                        await asyncio.sleep(0.005)
                prefix = "Already completed factual report. "
                text = prefix + "a" * (3989 - len(prefix)) if task["id"] == "K1" else self.result
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": text}])
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                                                       "reason": "Checked the current bound result."}])
            return normal(agent, context, cwd)

        self.native.handler = three_tasks
        validate = self.service.store._team.validate

        def old_validate(db, job, commands):
            for command in commands or []:
                if command.get("type") == "complete":
                    checked_text(command["result"], 4000)
            return validate(db, job, commands)

        finish = self.service.store._team.finish
        def old_finish(db, job, commands, **options):
            # Persist the retired whole-team stop contract; current formatting
            # rejections deliberately block only the offending task.
            options['local_rejection'] = False
            return finish(db, job, commands, **options)
        with patch.object(self.service.store._team, "validate", side_effect=old_validate), \
                patch.object(self.service.store._team, 'finish', side_effect=old_finish):
            created = self.create()
            failed = self.settled(created["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "invalid_text", failed["team"])
        self.assertEqual([job["error"] for job in failed["jobs"] if job["status"] == "failed"], ["invalid_text", "invalid_text"])
        room_id, current = failed["room"]["id"], failed["current_run"]
        body = {"text": "继续", "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"]}}
        with patch.object(self.service.store._team, "_host_text_failure_source", return_value=None):
            code, response = self.request("POST", f"/api/rooms/{room_id}/messages", body)
            self.assertEqual(code, 200, response)
            carried = self.settled(room_id)
            self.assertEqual(carried["objective"]["status"], "needs_user", carried["team"])
        return self.settled(room_id), failed

    def test_old_zero_call_carry_continues_same_board_without_replaying_finished_report(self):
        carried, failed = self.legacy_failure_and_carry()
        room_id, current = carried["room"]["id"], carried["current_run"]
        self.assertEqual(carried["objective"]["budget"]["used"], 0)
        self.assertEqual(carried["objective"]["status"], "interrupted")
        self.assertEqual(carried["objective"]["interruption_kind"], "host_result_validation")
        self.assertEqual(carried["collaboration"]["outcome"]["state"], "interrupted")
        outcome = carried["collaboration"]["outcome"]
        for field in ('reason', 'next_action'):
            self.assertEqual(outcome[field + '_i18n'], {'parts': [{'key': outcome[field], 'params': {}}]})
            self.assertIs(type(outcome[field]), str)
        self.assertTrue(carried["continuation"]["can_send"])
        tasks = {task["id"]: task for task in carried["team"]["tasks"]}
        self.assertEqual([(tasks[key]["status"], tasks[key]["result_version"], tasks[key]["assignment_epoch"])
                          for key in ("K1", "K2", "K3")], [("blocked", 1, 1), ("blocked", 0, 1), ("blocked", 0, 1)])
        self.assertEqual(tasks["K3"]["dependencies"], ["K1", "K2"])
        self.assertEqual(len(tasks["K1"]["result"]), 3989)
        original_rows = [(job["id"], job["status"], job["error"]) for job in failed["jobs"]]
        prior_phases = len(self.phases)
        body = {"text": "继续", "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"]}}
        code, response = self.request("POST", f"/api/rooms/{room_id}/messages", body)
        self.assertEqual(code, 200, response)
        final = self.settled(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(final["current_run"]["id"], current["id"])
        self.assertEqual(final["objective"]["id"], carried["objective"]["id"])
        self.assertEqual(final["room"]["runtime_settings"], carried["room"]["runtime_settings"])
        self.assertEqual([(job["id"], job["status"], job["error"]) for job in final["jobs"][:len(original_rows)]], original_rows)
        self.assertEqual({task["id"]: (task["owner"], task["assignment_epoch"]) for task in final["team"]["tasks"]},
                         {key: (task["owner"], task["assignment_epoch"]) for key, task in tasks.items()})
        resumed = self.phases[prior_phases:]
        self.assertNotIn(("work", "K1", 1), resumed)
        self.assertNotIn("plan", [phase[0] for phase in resumed])
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(sum(phase[:2] == ("work", "K2") for phase in resumed), 1)
        self.assertEqual(sum(phase[:2] == ("work", "K3") for phase in resumed), 1)
        calls = len(self.native.calls)
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/messages", body)[0], 200)
        self.assertEqual(len(self.native.calls), calls)

    def test_carry_provenance_rejects_changed_binding_owner_epoch_and_human_blocks(self):
        carried, failed = self.legacy_failure_and_carry()
        room_id, run_id = carried["room"]["id"], carried["current_run"]["id"]
        mutations = [
            ("UPDATE shared_tasks SET owner='claude' WHERE run_id=? AND id='K2'", (run_id,)),
            ("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE run_id=? AND id='K2'", (run_id,)),
            ("UPDATE shared_tasks SET result_version=result_version+1 WHERE run_id=? AND id='K2'", (run_id,)),
            ("UPDATE shared_tasks SET block_cause='permission_required' WHERE run_id=? AND id='K1'", (run_id,)),
            ("UPDATE objectives SET continuation_json='{}' WHERE id=?", (carried["objective"]["id"],)),
            ("UPDATE jobs SET error='other_error' WHERE room_id=? AND error='invalid_text'", (room_id,)),
            ("UPDATE members SET identity_uncertain=1 WHERE room_id=? AND provider='codex'", (room_id,)),
        ]
        calls = len(self.native.calls)
        with self.service.store._connection(write=True) as db:
            for statement, parameters in mutations:
                with self.subTest(statement=statement):
                    db.execute("SAVEPOINT invalid_source")
                    db.execute(statement, parameters)
                    self.assertFalse(self.service.store._team.resume_host_text_failure(db, room_id))
                    db.execute("ROLLBACK TO invalid_source")
                    db.execute("RELEASE invalid_source")
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.service.store.snapshot(room_id)["objective"]["status"], "needs_user")


class LongTeamTextValidationTests(unittest.TestCase):
    setUp = store_fixtures.TeamStoreTests.setUp
    tearDown = store_fixtures.TeamStoreTests.tearDown
    room = store_fixtures.TeamStoreTests.room
    finish = store_fixtures.TeamStoreTests.finish
    task = store_fixtures.TeamStoreTests.task
    plan = store_fixtures.TeamStoreTests.plan
    assertCode = store_fixtures.TeamStoreTests.assertCode

    def test_complete_and_help_accept_long_utf8_but_reject_invalid_or_oversize_text(self):
        room = self.plan()
        job = self.store.claim(room, "claude")
        for kind, field, extra in (("complete", "result", {}), ("request_help", "text", {"to": "codex", "capability": "text"})):
            command = {"type": kind, "task_id": "main", field: "事实与边界。" * 2000, **extra}
            self.assertTrue(self.store.prevalidate_team_result(job["id"], [command]))
            for value in (None, 1, False, [], "", " \n", "unsafe\0text", "bad\ud800"):
                with self.subTest(kind=kind, value_type=type(value).__name__):
                    self.assertCode("invalid_text", lambda: self.store.prevalidate_team_result(job["id"], [{**command, field: value}]))
            self.assertCode("message_too_large", lambda: self.store.prevalidate_team_result(
                job["id"], [{**command, field: "a" * (MAX_MESSAGE_UTF8_BYTES + 1)}]))
        # Removing a field's hidden cap does not change the existing response budget.
        envelope = json.dumps({"message": "Report", "state": "done", "commands": [{"type": "complete", "task_id": "main", "result": "a" * (32 * 1024)}]})
        self.assertCode("invalid_agent_response", lambda: _response(envelope, "claude", ("claude", "codex"), team=True))

    def test_long_result_does_not_bypass_owner_version_or_assignment_cas(self):
        room = self.plan()
        job = self.store.claim(room, "claude")
        command = {"type": "complete", "task_id": "main", "result": "Valid result. " * 800}
        for column, value, code in (("owner", "codex", "team_context_changed"),
                                    ("result_version", 1, "team_result_changed"),
                                    ("assignment_epoch", 1, "team_assignment_changed")):
            with self.subTest(column=column), self.store._connection(write=True) as db:
                db.execute("SAVEPOINT stale_binding")
                db.execute(f"UPDATE shared_tasks SET {column}=? WHERE room_id=? AND id='main'", (value, room))
                with self.assertRaises(RoomError) as denied:
                    self.store._team.validate(db, db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone(), [command])
                self.assertEqual(denied.exception.code, code)
                db.execute("ROLLBACK TO stale_binding")
                db.execute("RELEASE stale_binding")
        self.assertIsNone(self.task(room, "main")["result"])

    def test_legacy_model_block_named_invalid_text_is_not_a_host_failure(self):
        room = self.plan()
        actor = self.store.claim(room, "claude")
        peer = self.store.claim(room, "codex")
        self.finish(actor, [{"type": "block", "task_id": "main", "reason": "invalid_text", "cause": None}])
        with self.store._connection(write=True) as db:
            # Old blocks predate block_cause; their real team_command provenance survives.
            db.execute("UPDATE shared_tasks SET block_cause=NULL WHERE room_id=? AND id='main'", (room,))
        self.store.finish_job(peer["id"], error="invalid_text")
        with self.store._connection() as db:
            run_id = self.store._team.team(db, room)["current_run_id"]
            self.assertIsNone(self.store._team._host_text_failure_source(db, room, run_id))
        self.assertEqual(self.task(room, "main")["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
