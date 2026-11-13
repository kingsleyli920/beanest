"""Prepared lineage projection regressions; constructed SQLite records, no models.

The asynchronous boundary uses synthetic native transport and the real private
workspace. No result in this fixture is evidence about a real provider/project.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.room_store import RoomStore
import test_native_room_service as native


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ObjectiveDeliveryLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RoomStore(Path(self.temp.name) / "rooms.sqlite3")
        self.room = self.new_room()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def new_room(self):
        return self.store.create_room("Finish the same original deliverable.", "claude", str(uuid4()),
            collaboration=True, mode="python_artifacts", work_mode="build", execution_policy={"mode": "autonomous"})[0]

    def current(self, room=None):
        value = self.store.snapshot(room or self.room)
        return value["current_run"]["id"], value["objective"]["id"]

    def evidence(self, path, content, *, task="T1", status="accepted", inherited=None, room=None):
        room = room or self.room
        run, _ = self.current(room)
        number = self.store.snapshot(room)["current_run"]["number"]
        file = {"path": path, "sha256": digest(content)}
        kind = "self_check" if status == "self_checked" else "independent_review"
        with self.store._connection(write=True) as db:
            db.execute("UPDATE team_runs SET artifact_revision=?,artifact_digest=? WHERE id=?", (number, digest(run), run))
            db.execute("INSERT INTO shared_tasks(room_id,run_id,id,title,description,acceptance,dependencies_json,write_paths_json,"
                "capability,status,owner,result,result_version,result_revision,result_digest,result_files_json,review_kind,inherited_from_run) "
                "VALUES (?,?,?,?,?,?,'[]',?,'text',?,'claude',?,1,?,?,?,?,?)",
                (room, run, task, task, "Constructed projection fixture.", "Exact prior result.", json.dumps([path]), status,
                 "Constructed task result.", number, digest(run), json.dumps([file]), kind, inherited))
        return {**file, "run_id": run, "task_id": task, "revision": number, "digest": digest(run), "review_kind": kind}

    def next_objective(self, *, carry=True, room=None):
        room = room or self.room
        previous, _ = self.current(room)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE room_id=? AND status='queued'", (room,))
            db.execute("UPDATE teams SET phase='ready_for_user' WHERE room_id=?", (room,))
            number = db.execute("SELECT number FROM team_runs WHERE id=?", (previous,)).fetchone()[0]
            target = self.store._team.start_next(db, room, "Finish the same original deliverable.", "claude", previous,
                number, digest(previous), retry=False, execution_policy={"mode": "autonomous"}, assessment=True)
            if carry:
                self.store._team.carry_unfinished(db, room, previous, target)
        return target, self.current(room)[1]

    def set_continuation(self, objective, value):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE objectives SET continuation_json=? WHERE id=?", (json.dumps(value), objective))

    def test_original_same_objective_deliveries_remain_unchanged(self):
        expected = self.evidence("main.py", "version one")
        self.assertEqual(self.store.objective_deliveries(self.room), [expected])

    def test_repeated_recovery_keeps_original_peer_and_self_checked_files_without_counting_clones(self):
        first = self.evidence("main.py", "version one")
        checked = self.evidence("notes.md", "checked notes", task="T2", status="self_checked")
        self.next_objective()
        self.next_objective()
        self.assertEqual(self.store.objective_deliveries(self.room), [first, checked])
        self.assertEqual(checked["review_kind"], "self_check")

    def test_latest_real_result_wins_path_while_inherited_clone_and_inventory_never_become_delivery(self):
        self.evidence("main.py", "version one")
        notes = self.evidence("notes.md", "original notes", task="T2")
        self.next_objective()
        latest = self.evidence("main.py", "version two", task="T3", status="self_checked")
        self.next_objective()
        self.evidence("not-reviewed.txt", "working result", task="W", status="review")
        self.evidence("inventory.txt", "untrusted inherited file", task="I", inherited=self.current()[0])
        with self.store._connection(write=True) as db:
            self.store._event(db, self.room, "native_partial_inspected", sender="claude", task_id="inspection-only",
                outcome="complete", note="A discussion note is not an implementation result.", canonical_imported=False)
        self.assertEqual(self.store.objective_deliveries(self.room), [latest, notes])

    def test_unrelated_equal_goal_and_user_followup_do_not_merge_prior_deliveries(self):
        self.evidence("prior.py", "old delivery")
        source, _ = self.current()
        self.next_objective(carry=False)
        current = self.evidence("current.py", "new direct request")
        _, objective = self.current()
        for value in (None, {"kind": "user_followup", "source_run_id": source, "user_request": "New requirement."},
                      {"scope": "new_goal", "source_run_id": source}):
            with self.subTest(continuation=value):
                self.set_continuation(objective, value)
                self.assertEqual(self.store.objective_deliveries(self.room), [current])

    def test_broken_cyclic_foreign_or_forward_lineage_rejects_ancestry_but_keeps_own_result(self):
        self.evidence("prior.py", "old delivery")
        first_run, first_objective = self.current()
        _, middle_objective = self.next_objective()
        self.evidence("middle.py", "middle real result", task="M")
        middle_run, _ = self.current()
        _, objective = self.next_objective()
        current = self.evidence("own.py", "current real result", task="C")
        own_run, _ = self.current()
        foreign = self.new_room()
        foreign_run, _ = self.current(foreign)
        for source in (str(uuid4()), foreign_run, own_run, None, {}, [], 1):
            with self.subTest(source=source):
                self.set_continuation(objective, {"scope": "unfinished_work_only", "source_run_id": source})
                self.assertEqual(self.store.objective_deliveries(self.room), [current])
        self.set_continuation(objective, {"scope": "unfinished_work_only", "source_run_id": middle_run})
        for broken in (str(uuid4()), own_run, middle_run):
            with self.subTest(deeper_source=broken):
                self.set_continuation(middle_objective, {"scope": "unfinished_work_only", "source_run_id": broken})
                self.assertEqual(self.store.objective_deliveries(self.room), [current])
        self.set_continuation(middle_objective, {"scope": "unfinished_work_only", "source_run_id": first_run})
        self.set_continuation(first_objective, {"scope": "unfinished_work_only", "source_run_id": own_run})
        self.assertEqual(self.store.objective_deliveries(self.room), [current])

    def test_linked_unfinished_work_stops_at_a_legitimate_new_goal_boundary(self):
        self.evidence("older-goal.py", "earlier completed goal")
        older_run, _ = self.current()
        _, new_goal = self.next_objective(carry=False)
        self.set_continuation(new_goal, {"kind": "user_followup", "source_run_id": older_run,
                                        "user_request": "A separate new requirement."})
        expected = self.evidence("new-goal.py", "completed part of the new requirement")
        self.next_objective()
        self.assertEqual(self.store.objective_deliveries(self.room), [expected])

    def test_registered_initial_native_handoff_is_a_boundary_even_after_source_deletion(self):
        for source_exists in (True, False):
            with self.subTest(source_exists=source_exists):
                source_room = self.new_room() if source_exists else str(uuid4())
                source_run = self.current(source_room)[0] if source_exists else str(uuid4())
                if source_exists:
                    self.evidence("foreign.py", "old room delivery", room=source_room)
                self.room = self.new_room()
                expected = self.evidence("native-own.py", "own original native delivery")
                _, original_objective = self.current()
                self.set_continuation(original_objective, {"scope": "unfinished_work_only", "source_run_id": source_run})
                with self.store._connection(write=True) as db:
                    db.execute("UPDATE rooms SET mode='native_project' WHERE id=?", (self.room,))
                    db.execute("INSERT INTO native_handoffs VALUES (?,?,?,?,?)", (self.room, source_room, source_run, 1, digest(source_run)))
                self.next_objective()
                self.next_objective()
                self.assertEqual(self.store.objective_deliveries(self.room), [expected])

    def test_long_recovery_chain_has_no_fixed_stage_cutoff(self):
        expected = self.evidence("main.py", "oldest real delivery")
        for _ in range(24):
            self.next_objective()
        connection = self.store._connection

        @contextmanager
        def limited_connection(*args, **kwargs):
            with connection(*args, **kwargs) as db:
                db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 16)
                yield db

        # The verified chain may grow; each query must retain a fixed shape.
        with patch.object(self.store, "_connection", side_effect=limited_connection):
            self.assertEqual(self.store.objective_deliveries(self.room), [expected])

    def test_ancestor_results_after_the_exact_source_run_boundary_are_excluded(self):
        first = self.evidence("main.py", "referenced version")
        source_run, source_objective = self.current()
        self.next_objective()
        current_run, _ = self.current()
        with self.store._connection(write=True) as db:
            future = self.store._team._create_run(db, self.room, 3)
            db.execute("UPDATE team_runs SET objective_id=? WHERE id=?", (source_objective, future))
        self.evidence("main.py", "future branch", task="F")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE teams SET current_run_id=? WHERE room_id=?", (current_run, self.room))
        self.assertNotEqual(source_run, future)
        self.assertEqual(self.store.objective_deliveries(self.room), [first])


class DeliveryLineageCurrentProofTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create

    async def prepared_origin(self):
        created = await self.create()
        await self.settle()
        room = created["room"]["id"]
        before = await self.service.read_room(room)
        expected = copy.deepcopy(before["objective"]["delivery"]["files"])
        original_goal = before["objective"]["original_goal"]
        binding = before["current_run"]
        with self.service.store._connection(write=True) as db:
            target = self.service.store._team.start_next(db, room, original_goal, "claude", binding["id"],
                binding["revision"], binding["digest"], retry=False, execution_policy={"mode": "autonomous"}, assessment=True)
            self.service.store._team.carry_unfinished(db, room, binding["id"], target)
        return room, expected

    async def test_real_workspace_sha_still_controls_visibility_and_original_file_reference_is_readable(self):
        room, expected = await self.prepared_origin()
        resumed = await self.service.read_room(room)
        self.assertEqual(resumed["objective"]["delivery"]["files"], expected)
        await self.check_current_proof(room, expected)

    async def test_existing_service_sha_proof_control_with_injected_exact_origin_not_inventory(self):
        room, expected = await self.prepared_origin()
        origins = [{key: value for key, value in file.items() if key != "verified"} for file in expected]
        # Isolate the unchanged verifier until the lineage query is implemented.
        # This injected origin is not evidence that production lineage works.
        with patch.object(self.service.store, "objective_deliveries", return_value=origins):
            self.assertEqual((await self.service.read_room(room))["objective"]["delivery"]["files"], expected)
            await self.check_current_proof(room, expected)

    async def test_a_file_an_unfinished_task_is_reworking_is_not_a_changed_delivery(self):
        # Catches the real room flagging 部分目标成果与已交付版本不一致 while a repair task was still reworking
        # a file an earlier task delivered; the mismatch belongs to the end check, not the middle of the run.
        room, expected = await self.prepared_origin()
        main = next(file for file in expected if file["path"] == "main.py")
        workspace = self.service._project(room)
        workspace.apply("claude", str(uuid4()), workspace.snapshot()["revision"],
                        [{"path": "main.py", "previous_sha256": main["sha256"], "content": "VALUE = 99\n"}])
        store = self.service.store
        with store._connection(write=True) as db:
            run = store._team.team(db, room)["current_run_id"]
            row = dict(db.execute("SELECT * FROM shared_tasks WHERE room_id=? LIMIT 1", (room,)).fetchone())
            row.update(id="rework", run_id=run, status="assigned", owner="claude", write_paths_json='["main.py"]',
                       dependencies_json="[]", result_version=0, result_files_json=None, inherited_from_run=None)
            db.execute("INSERT INTO shared_tasks(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", list(row.values()))
        delivery = (await self.service.read_room(room))["objective"]["delivery"]
        self.assertEqual((delivery["verified"], delivery["missing_or_changed"]), (True, []))
        self.assertTrue(next(file for file in delivery["files"] if file["path"] == "main.py")["reworking"])

    async def check_current_proof(self, room, expected):
        main = next(file for file in expected if file["path"] == "main.py")
        content = await self.service.read_delivery_file(room, **{key:main[key] for key in ("run_id", "revision", "digest", "path", "sha256")})
        self.assertEqual(content["content"], "VALUE = 2\n")
        workspace = self.service._project(room)
        workspace.apply("claude", str(uuid4()), workspace.snapshot()["revision"],
                        [{"path": "main.py", "previous_sha256": main["sha256"], "content": "VALUE = 99\n"}])
        changed = await self.service.read_room(room)
        self.assertFalse(changed["objective"]["delivery"]["verified"])
        self.assertEqual(changed["objective"]["delivery"]["missing_or_changed"],
                         [{"path": "main.py", "expected_sha256": main["sha256"], "reason": "changed"}])


class DeliveryLineageHTTPTests(unittest.TestCase):
    """Public HTTP recovery and reader boundaries with a synthetic executor."""
    from test_native_room_http import NativeRoomHTTPTests as _HTTPFixture
    setUp = _HTTPFixture.setUp
    tearDown = _HTTPFixture.tearDown
    request = _HTTPFixture.request
    configure = _HTTPFixture.configure
    wait = _HTTPFixture.wait
    handler = native.NativeRoomServiceTests.handler

    def recovered_with_ancestor(self):
        from test_team_store import proposal
        self.source, project = self.configure()
        self.assertIsNone(self.service.partner_manager)
        def handler(agent, context, cwd):
            phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands += [proposal("T1", paths=["main.py"]),
                                 proposal("T2", paths=["notes.md"], dependencies=["T1"])]
                own = "T1" if agent == "claude" else "T2"
                if commands or any(item["id"] == own and item["owner"] is None for item in board["tasks"]):
                    commands.append({"type": "claim", "task_id": own})
                return native.native_reply(context, commands + [{"type": "plan_ready"}])
            if phase == "work" and task["id"] == "T1":
                (cwd / "main.py").write_text("VALUE = 2\n")
                return native.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Concrete first part completed."}])
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact first-part SHA."}])
            self.assertEqual((phase, task["id"]), ("work", "T2"))
            (cwd / "notes.md").write_text("Retained unfinished second part.\n")
            self.native.failure = {"status": "failed", "failure_code": "protocol_turn_incomplete"}
            return native.native_reply(context)
        self.native.handler = handler
        code, created = self.request("POST", "/api/rooms", {"goal": "Update the value and the notes.", "target": "claude",
            "request_id": "lineage-http-create", "mode": "native_project", "project_id": project,
            "participants": ["claude", "codex"], "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        self.room = created["room"]["id"]
        before = self.wait(self.room, lambda item: item["current_run"]["can_recover_native"])
        self.origin = copy.deepcopy(before["objective"]["delivery"]["files"][0])
        self.assertEqual(self.origin["path"], "main.py")
        self.native.failure = None
        # Keep the next model calls queued so the HTTP/UI proof observes just
        # the acknowledged recovery and original delivery, not new work.
        self.service._wake = lambda _: None
        binding = before["current_run"]
        code, recovered = self.request("POST", f"/api/rooms/{self.room}/recover-native", {
            "request_id": "lineage-http-recover", "run_id": binding["id"], "revision": binding["revision"], "digest": binding["digest"]})
        self.assertEqual(code, 200, recovered)
        self.assertNotEqual(recovered["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual(recovered["objective"]["delivery"]["files"], [self.origin])
        return recovered

    def change_current_head(self):
        workspace = self.service._project(self.room)
        workspace.apply("claude", str(uuid4()), workspace.snapshot()["revision"],
            [{"path": "main.py", "previous_sha256": self.origin["sha256"], "content": "VALUE = 99\n"}])
        code, changed = self.request("GET", f"/api/rooms/{self.room}")
        self.assertEqual(code, 200)
        self.assertFalse(changed["objective"]["delivery"]["verified"])
        self.assertEqual(changed["objective"]["delivery"]["missing_or_changed"],
            [{"path": "main.py", "expected_sha256": self.origin["sha256"], "reason": "changed"}])
        return changed

    def test_user_get_keeps_original_card_reference_and_current_sha_verification_after_recovery(self):
        from urllib.parse import urlencode
        self.recovered_with_ancestor()
        code, visible = self.request("GET", f"/api/rooms/{self.room}")
        self.assertEqual(code, 200)
        self.assertEqual(visible["objective"]["delivery"]["files"], [self.origin])
        query = {key:self.origin[key] for key in ("run_id", "revision", "digest", "path", "sha256")}
        count = len(self.native.calls)
        code, body = self.request("GET", f"/api/rooms/{self.room}/file?" + urlencode(query))
        self.assertEqual((code, body["content"]), (200, "VALUE = 2\n"))
        code, denied = self.request("GET", f"/api/rooms/{self.room}/file?" + urlencode(query | {"sha256": "0" * 64}))
        self.assertEqual((code, denied["error"]), (409, "objective_binding_changed"))
        self.change_current_head()
        self.assertEqual(len(self.native.calls), count)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.assertEqual((self.source / "notes.md").read_text(), "Original notes.\n")
