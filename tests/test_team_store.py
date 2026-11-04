import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore


def proposal(task_id, *, dependencies=None, paths=None, capability="text"):
    return {"type": "propose", "id": task_id, "title": "Deliver " + task_id,
            "description": "Implement a small independently reviewable part.",
            "acceptance": "A peer checks the concrete result and its evidence.",
            "dependencies": dependencies or [], "write_paths": paths or [], "capability": capability}


class TeamStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "rooms.sqlite3"
        self.now = 1000.0
        self.store = RoomStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def room(self, **kwargs):
        return self.store.create_room("A shared deliverable", "claude", str(uuid4()), collaboration=True, **kwargs)[0]

    def finish(self, job, commands, **kwargs):
        self.assertTrue(self.store.prevalidate_team_result(job["id"], commands))
        self.store.finish_job(job["id"], message="Concrete response", team_commands=commands, **kwargs)

    def task(self, room_id, task_id):
        return next(task for task in self.store.snapshot(room_id)["team"]["tasks"] if task["id"] == task_id)

    def plan(self, *, dependency=False, paths=False):
        room_id = self.room()
        author = self.store.claim(room_id, "claude")
        peer = self.store.claim(room_id, "codex")
        self.finish(author, [proposal("main", paths=["main.py"] if paths else []),
                             proposal("tests", dependencies=["main"] if dependency else [], paths=["test_main.py"] if paths else []),
                             {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        self.finish(peer, [{"type": "claim", "task_id": "tests"}, {"type": "plan_ready"}])
        confirmation = self.store.claim(room_id, "codex")
        self.assertEqual(confirmation["kind"], "team_coordinate")
        self.finish(confirmation, [{"type": "plan_ready"}])
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "working")
        return room_id

    def assertCode(self, expected, operation):
        with self.assertRaises(RoomError) as error:
            operation()
        self.assertEqual(error.exception.code, expected)

    def test_missing_confirmation_prompts_only_ready_then_queues_independent_work(self):
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now,
            agents={"claude":"Claude", "kimi":"Kimi", "codex":"Codex"})
        for repair in (False, True):
            with self.subTest(repair=repair):
                room_id = self.room()
                plans = {provider:self.store.claim(room_id, provider) for provider in ("claude", "kimi", "codex")}
                self.finish(plans["claude"], [proposal("T1"), proposal("T2"),
                    proposal("T3", dependencies=["T1", "T2"]),
                    {"type":"claim", "task_id":"T2"}, {"type":"plan_ready"}])
                self.finish(plans["codex"], [{"type":"claim", "task_id":"T3"}, {"type":"plan_ready"}])
                self.finish(plans["kimi"], [{"type":"claim", "task_id":"T1"}])
                for provider in ("claude", "codex"):
                    coordinate = self.store.claim(room_id, provider)
                    if coordinate:
                        self.assertEqual(coordinate["kind"], "team_coordinate")
                        self.finish(coordinate, [{"type":"plan_ready"}])
                coordinate = self.store.claim(room_id, "kimi")
                if repair:
                    self.finish(coordinate, [])
                    coordinate = self.store.claim(room_id, "kimi")
                    self.assertIn("The shared plan is stalled", coordinate["text"])
                self.assertIn('Unclaimed task IDs: []', coordinate["text"])
                self.assertIn('Do not issue claim commands or repeat existing claims', coordinate["text"])
                self.assertIn('Your only missing action is the command {"type":"plan_ready"}', coordinate["text"])
                self.assertIn('"owner": "kimi"', coordinate["text"])
                self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "planning")
                self.finish(coordinate, [{"type":"plan_ready"}])
                queued = self.store.snapshot(room_id)
                self.assertEqual(queued["team"]["phase"], "working")
                self.assertEqual(sum(job["kind"] == "team_work" and job["status"] == "queued" for job in queued["jobs"]), 2)
                self.assertEqual(self.store.claim(room_id, "kimi")["team"]["task_id"], "T1")
                self.assertEqual(self.store.claim(room_id, "claude")["team"]["task_id"], "T2")
                self.assertIsNone(self.store.claim(room_id, "codex"))
                self.assertEqual(self.task(room_id, "T3")["dependencies"], ["T1", "T2"])

    def test_open_coordination_prompt_lists_only_unowned_ids_and_existing_owners(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("owned"), proposal("open"),
            {"type":"claim", "task_id":"owned"}, {"type":"plan_ready"}])
        self.finish(peer, [])
        coordinate = self.store.claim(room_id, "codex")
        self.assertIn('Unclaimed task IDs: ["open"]', coordinate["text"])
        self.assertIn('"id": "owned", "owner": "claude"', coordinate["text"])
        self.assertIn('Claim only useful unclaimed task IDs listed above', coordinate["text"])
        self.assertIn('explicitly return the command {"type":"plan_ready"}', coordinate["text"])

    def test_opt_in_limits_idempotency_and_legacy_compatibility(self):
        room_id, fresh = self.store.create_room("Goal", "all", "team-key", collaboration=True, mode="python_artifacts")
        self.assertTrue(fresh)
        self.assertEqual(self.store.create_room("Goal", "all", "team-key", collaboration=True, mode="python_artifacts"), (room_id, False))
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["budget"], {"used": 0, "limit": None, "deadline": None})
        self.assertEqual(snapshot["team"]["plan_author"], "claude")
        self.assertEqual([job["kind"] for job in snapshot["jobs"]], ["team_plan", "team_plan"])
        old, _ = self.store.create_room("Old room", "claude", "legacy-key")
        self.assertIsNone(self.store.snapshot(old)["team"])
        self.assertIsNone(self.store.snapshot(old)["budget"]["limit"])
        listed = {room["id"]: room for room in self.store.list_rooms()["rooms"]}
        self.assertTrue(listed[room_id]["collaboration"])
        self.assertNotIn("collaboration", listed[old])
        self.assertCode("request_conflict", lambda: self.store.create_room("Goal", "claude", "team-key"))
        self.assertCode("project_requires_first_author", lambda: self.store.create_room("Old", "all", "old-project", mode="python_artifacts"))

    def test_obsolete_team_budget_configuration_does_not_limit_new_tasks(self):
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now, team_max_calls=7, team_timeout=125.5)
        room_id = self.room()
        expected = {"used": 0, "limit": None, "deadline": None}
        self.assertEqual(self.store.snapshot(room_id)["budget"], expected)
        with self.store._connection() as db:
            row = db.execute("SELECT call_limit,deadline FROM rooms WHERE id=?", (room_id,)).fetchone()
            self.assertEqual(tuple(row), (0, 0))
        legacy_id, _ = self.store.create_room("Legacy defaults", "claude", "legacy-budget")
        self.assertEqual(self.store.snapshot(legacy_id)["budget"],
                         {"used": 0, "limit": None, "deadline": None})

    def test_new_budget_configuration_does_not_reset_an_idempotent_room(self):
        original, _ = self.store.create_room("Existing team", "all", "frozen-team", collaboration=True)
        before = self.store.snapshot(original)
        self.store.close()
        self.now += 300
        self.store = RoomStore(self.path, clock=lambda: self.now, team_max_calls=29, team_timeout=60)
        self.assertEqual(self.store.create_room("Existing team", "all", "frozen-team", collaboration=True),
                         (original, False))
        after = self.store.snapshot(original)
        self.assertEqual(after["budget"], before["budget"])
        self.assertEqual(after["jobs"], before["jobs"])
        fresh = self.room()
        self.assertEqual(self.store.snapshot(fresh)["budget"],
                         {"used": 0, "limit": None, "deadline": None})

    def test_team_budget_configuration_rejects_invalid_types_and_unbounded_values(self):
        invalid_path = self.path.parent / "invalid.sqlite3"
        invalid_calls = (True, False, None, 1, 0, -1, 33, 2.0, "2", 10 ** 100)
        invalid_timeouts = (True, False, None, 0, -1, 7200.1, float("inf"), float("-inf"),
                            float("nan"), "60", 1j, 10 ** 1000)
        for field, values in (("team_max_calls", invalid_calls), ("team_timeout", invalid_timeouts)):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertCode("invalid_configuration", lambda: RoomStore(invalid_path, **{field: value}))
                    self.assertFalse(invalid_path.exists())
        minimum = RoomStore(invalid_path, team_max_calls=2, team_timeout=0.5, clock=lambda: self.now)
        try:
            room_id, _ = minimum.create_room("Minimum bounded team", "all", "minimum", collaboration=True)
            self.assertEqual(minimum.snapshot(room_id)["budget"],
                             {"used": 0, "limit": None, "deadline": None})
            author = minimum.claim(room_id, "claude")
            peer = minimum.claim(room_id, "codex")
            minimum.finish_job(author["id"], message="Proposed bounded work.", team_commands=[
                proposal("main"), {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
            minimum.finish_job(peer["id"], message="The budget is exhausted.", team_commands=[])
            saved = minimum.snapshot(room_id)
            self.assertEqual(saved["budget"]["used"], 2)
            self.assertIsNone(saved["team"]["stop_reason"])
            self.assertTrue(any(job["status"] == "queued" for job in saved["jobs"]))
        finally:
            minimum.close()

    def test_planning_barrier_reconfirms_changed_plan_without_premature_work(self):
        room_id = self.room()
        author = self.store.claim(room_id, "claude")
        peer = self.store.claim(room_id, "codex")
        self.finish(peer, [{"type": "plan_ready"}])
        self.finish(author, [proposal("main"), {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["phase"], "planning")
        self.assertFalse(any(job["kind"] == "team_work" for job in snapshot["jobs"]))
        coord = self.store.claim(room_id, "codex")
        self.finish(coord, [{"type": "plan_ready"}])
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "working")

    def test_distinct_tasks_execute_in_parallel_with_member_singleflight(self):
        room_id = self.plan(paths=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda provider: self.store.claim(room_id, provider), ["claude", "claude", "codex", "codex"]))
        claimed = [job for job in results if job]
        self.assertEqual(len(claimed), 2)
        self.assertEqual({job["team"]["task_id"] for job in claimed}, {"main", "tests"})
        self.assertEqual([self.task(room_id, task)["status"] for task in ("main", "tests")], ["working", "working"])
        before = len(self.store.snapshot(room_id)["jobs"])
        for _ in range(3):
            self.store.reconcile_team(room_id)
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), before)

    def test_independent_exact_version_reviews_are_required_for_ready_for_user(self):
        room_id = self.plan(paths=True)
        left, right = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(left, [{"type": "complete", "task_id": "main", "result": "Main implementation"}],
                    artifact_revision=1, artifact_digest="a" * 64, artifact_files=[{"path": "main.py", "sha256": "b" * 64}])
        self.assertEqual(self.task(room_id, "main")["status"], "review")
        self.assertNotEqual(self.store.snapshot(room_id)["room"]["status"], "ready_for_user")
        self.finish(right, [{"type": "complete", "task_id": "tests", "result": "Independent tests"}], artifact_revision=2, artifact_digest="c" * 64)
        reviews = [self.store.claim(room_id, provider) for provider in ("claude", "codex")]
        self.assertTrue(all(job["kind"] == "team_review" for job in reviews))
        for job in reviews:
            task_id = job["team"]["task_id"]
            self.assertNotEqual(self.task(room_id, task_id)["owner"], job["provider"])
            self.finish(job, [{"type": "review", "task_id": task_id, "verdict": "accept", "reason": "Checked exact result and file hashes."}])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "ready_for_user")
        self.assertTrue(all(task["status"] == "accepted" for task in snapshot["team"]["tasks"]))
        self.assertEqual(self.task(room_id, "main")["result_files"], [{"path": "main.py", "sha256": "b" * 64}])
        self.assertFalse(any(job["kind"] in {"feedback", "review"} for job in snapshot["jobs"]))

    def test_dependency_waits_for_acceptance_not_merely_result(self):
        room_id = self.plan(dependency=True)
        self.assertIsNone(self.store.claim(room_id, "codex"))
        work = self.store.claim(room_id, "claude")
        self.finish(work, [{"type": "complete", "task_id": "main", "result": "Main"}])
        review = self.store.claim(room_id, "codex")
        self.assertEqual(review["kind"], "team_review")
        self.finish(review, [{"type": "review", "task_id": "main", "verdict": "accept", "reason": "Checked"}])
        next_job = self.store.claim(room_id, "codex")
        self.assertEqual((next_job["kind"], next_job["team"]["task_id"]), ("team_work", "tests"))

    def test_help_is_bound_and_resumes_owner_once(self):
        room_id = self.plan(dependency=True)
        work = self.store.claim(room_id, "claude")
        self.finish(work, [{"type": "request_help", "task_id": "main", "to": "codex", "capability": "text", "text": "Check the edge case."}])
        self.assertEqual(self.task(room_id, "main")["status"], "waiting_peer")
        help_job = self.store.claim(room_id, "codex")
        self.assertEqual(help_job["kind"], "team_help")
        self.finish(help_job, [])
        resumed = self.store.claim(room_id, "claude")
        self.assertEqual((resumed["kind"], resumed["team"]["task_id"]), ("team_work", "main"))
        self.assertEqual(self.task(room_id, "main")["help_result"], "Concrete response")

    def test_unavailable_image_help_blocks_without_fake_dispatch(self):
        room_id = self.plan(dependency=True)
        work = self.store.claim(room_id, "claude")
        self.finish(work, [{"type": "request_help", "task_id": "main", "to": "codex", "capability": "image", "text": "Render the sprite."}])
        task = self.task(room_id, "main")
        self.assertEqual((task["status"], task["blocked_reason"], task["help_text"]), ("blocked", "capability_unavailable", "Render the sprite."))
        self.assertIsNone(self.store.claim(room_id, "codex"))
        self.assertEqual(self.store.snapshot(room_id)["room"]["status"], "needs_user")

    def test_empty_queue_and_unclaimed_tasks_do_not_complete_or_loop(self):
        room_id = self.room()
        author = self.store.claim(room_id, "claude")
        self.finish(author, [proposal("main"), {"type": "plan_ready"}])
        self.finish(self.store.claim(room_id, "codex"), [])
        coordinated = []
        for _ in range(3):
            for provider in ("claude", "codex"):
                coord = self.store.claim(room_id, provider)
                if coord:
                    self.assertEqual(coord["kind"], "team_coordinate")
                    coordinated.append(provider)
                    self.finish(coord, [{"type": "plan_ready"}])
        self.assertCountEqual(coordinated, ["claude", "codex", "claude", "codex"])
        count = len(self.store.snapshot(room_id)["jobs"])
        for _ in range(4):
            self.store.reconcile_team(room_id)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(len(snapshot["jobs"]), count)
        self.assertEqual(snapshot["room"]["status"], "needs_user")
        self.assertEqual(snapshot["team"]["phase"], "stopped")
        self.assertEqual(snapshot["team"]["stop_reason"], "team_plan_stalled")

    def test_five_tasks_can_be_claimed_one_per_coordinate_without_planning_stall(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [*[proposal(f"part{number}") for number in range(5)],
                             {"type": "claim", "task_id": "part0"}, {"type": "plan_ready"}])
        self.finish(peer, [])
        coordinate_count = 0
        for _ in range(8):
            if self.store.snapshot(room_id)["team"]["phase"] == "working":
                break
            progressed = False
            for provider in ("claude", "codex"):
                job = self.store.claim(room_id, provider)
                if not job:
                    continue
                progressed = True
                self.assertEqual(job["kind"], "team_coordinate")
                board = self.store.snapshot(room_id)["team"]
                remaining = next((task for task in board["tasks"] if task["status"] == "open"), None)
                commands = [{"type": "claim", "task_id": remaining["id"]}] if remaining else []
                if board["phase"] == "planning":
                    commands.append({"type": "plan_ready"})
                self.finish(job, commands)
                coordinate_count += 1
                if self.store.snapshot(room_id)["team"]["phase"] == "working":
                    break
            self.assertTrue(progressed)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["phase"], "working")
        self.assertEqual({task["status"] for task in snapshot["team"]["tasks"]}, {"assigned"})
        self.assertEqual({task["owner"] for task in snapshot["team"]["tasks"]}, {"claude", "codex"})
        self.assertGreaterEqual(coordinate_count, 4)
        count = len(snapshot["jobs"])
        for _ in range(3):
            self.store.snapshot(room_id)
            self.store.list_rooms()
            self.store.reconcile_team(room_id)
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), count)

    def test_delayed_same_revision_plan_confirmation_is_safe_after_work_starts(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("one"), proposal("two"), proposal("three"),
                             {"type": "claim", "task_id": "one"}, {"type": "plan_ready"}])
        self.finish(peer, [])
        author_coord = self.store.claim(room_id, "claude")
        peer_coord = self.store.claim(room_id, "codex")
        self.assertEqual(author_coord["kind"], "team_coordinate")
        self.assertEqual(peer_coord["kind"], "team_coordinate")
        self.finish(peer_coord, [{"type": "claim", "task_id": "two"},
                                {"type": "claim", "task_id": "three"}, {"type": "plan_ready"}])
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "working")
        self.finish(author_coord, [{"type": "plan_ready"}])
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "working")
        self.assertEqual(self.store.claim(room_id, "claude")["kind"], "team_work")

    def test_no_progress_confirmation_of_same_remaining_set_stops_without_extra_dispatch(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [*[proposal(f"part{number}") for number in range(5)],
                             {"type": "claim", "task_id": "part0"}, {"type": "plan_ready"}])
        self.finish(peer, [{"type": "claim", "task_id": "part1"}, {"type": "plan_ready"}])
        for _ in range(2):
            for provider in ("claude", "codex"):
                coordinate = self.store.claim(room_id, provider)
                self.assertEqual(coordinate["kind"], "team_coordinate")
                self.finish(coordinate, [{"type": "plan_ready"}])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["budget"]["used"], 6)
        self.assertEqual(snapshot["room"]["status"], "needs_user")
        self.assertEqual(snapshot["team"]["stop_reason"], "team_plan_stalled")
        self.assertEqual(sum(task["owner"] is None for task in snapshot["team"]["tasks"]), 3)
        for _ in range(4):
            self.store.snapshot(room_id)
            self.store.list_rooms()
            self.store.reconcile_team(room_id)
            self.assertIsNone(self.store.claim(room_id, "claude"))
            self.assertIsNone(self.store.claim(room_id, "codex"))
        self.assertEqual(self.store.snapshot(room_id)["jobs"], snapshot["jobs"])

    def test_explicit_coordination_repair_can_claim_dependent_text_tasks_and_start_work(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("code"), proposal("summary", dependencies=["code"]),
                             {"type": "claim", "task_id": "code"}, {"type": "plan_ready"}])
        self.finish(peer, [{"type": "plan_ready"}])
        for _ in range(8):
            for provider in ("claude", "codex"):
                job = self.store.claim(room_id, provider)
                if job is None:
                    continue
                self.assertEqual(job["kind"], "team_coordinate")
                commands = [{"type": "plan_ready"}]
                if provider == "codex" and "shared plan is stalled" in job["text"]:
                    self.assertIn('"summary"', job["text"])
                    commands.insert(0, {"type": "claim", "task_id": "summary"})
                self.finish(job, commands)
            if self.store.snapshot(room_id)["team"]["phase"] == "working":
                break
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["phase"], "working")
        self.assertEqual(self.task(room_id, "summary")["owner"], "codex")
        next_job = self.store.claim(room_id, "claude")
        if next_job["kind"] == "team_coordinate":
            self.finish(next_job, [{"type": "plan_ready"}])
            next_job = self.store.claim(room_id, "claude")
        self.assertEqual(next_job["kind"], "team_work")
        self.assertIsNone(self.store.claim(room_id, "codex"), "dependent work must still wait")
        self.assertGreaterEqual(sum(event["kind"] == "coordination_repair_queued" for event in snapshot["events"]), 1)

    def test_existing_team_member_schema_gains_signature_without_replaying_jobs(self):
        room_id = self.room()
        before = self.store.snapshot(room_id)
        with self.store._connection(write=True) as db:
            db.execute("ALTER TABLE team_members DROP COLUMN coordinated_signature")
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now)
        with self.store._connection() as db:
            columns = {column[1] for column in db.execute("PRAGMA table_info(team_members)")}
            self.assertIn("coordinated_signature", columns)
            self.assertIn("coordinated_revision", columns)
        self.assertEqual(self.store.snapshot(room_id)["jobs"], before["jobs"])

    def test_proposal_rejects_cycles_path_overlap_and_unsafe_paths_atomically(self):
        room_id = self.room()
        job = self.store.claim(room_id, "claude")
        for expected, commands in [
            ("invalid_team_dependencies", [proposal("a", dependencies=["b"]), proposal("b", dependencies=["a"])]),
            ("team_write_path_conflict", [proposal("a", paths=["one.py"]), proposal("b", paths=["ONE.py"])]),
            ("invalid_team_task", [proposal("a", paths=["../escape.py"])]),
        ]:
            self.assertCode(expected, lambda: self.store.prevalidate_team_result(job["id"], commands))
            self.assertEqual(self.store.snapshot(room_id)["team"]["tasks"], [])

    def test_peer_cannot_change_plan_or_complete_unowned_task(self):
        room_id = self.room()
        peer = self.store.claim(room_id, "codex")
        self.assertCode("team_plan_locked", lambda: self.store.prevalidate_team_result(peer["id"], [proposal("rogue")]))
        self.assertCode("team_task_permission", lambda: self.store.prevalidate_team_result(peer["id"], [{"type": "complete", "task_id": "rogue", "result": "No"}]))

    def test_revision_requests_are_bounded_and_stale_review_rejected(self):
        room_id = self.plan(dependency=True)
        for attempt in range(2):
            work = self.store.claim(room_id, "claude")
            self.finish(work, [{"type": "complete", "task_id": "main", "result": "Candidate"}])
            review = self.store.claim(room_id, "codex")
            if attempt == 0:
                with self.store._connection(write=True) as db:
                    db.execute("UPDATE team_jobs SET result_version=0 WHERE job_id=?", (review["id"],))
                self.assertCode("team_result_changed", lambda: self.store.prevalidate_team_result(review["id"], [{"type": "review", "task_id": "main", "verdict": "accept", "reason": "stale"}]))
                with self.store._connection(write=True) as db:
                    db.execute("UPDATE team_jobs SET result_version=1 WHERE job_id=?", (review["id"],))
            self.finish(review, [{"type": "review", "task_id": "main", "verdict": "changes_requested", "reason": "A concrete correction is required."}])
        self.assertEqual(self.task(room_id, "main")["status"], "blocked")
        self.assertEqual(self.task(room_id, "main")["blocked_reason"], "team_revision_limit")
        self.assertIsNone(self.store.claim(room_id, "claude"))

    def test_cancel_gates_late_results_and_does_not_requeue(self):
        room_id = self.plan()
        job = self.store.claim(room_id, "claude")
        self.store.request_stop(room_id, "cancel_requested")
        self.assertFalse(self.store.prevalidate_team_result(job["id"], [{"type": "complete", "task_id": "main", "result": "late"}]))
        self.store.finish_stop(room_id, confirmed=True)
        self.store.reconcile_team(room_id)
        self.assertIsNone(self.store.claim(room_id, "codex"))
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "cancelled")

    def test_restart_freezes_tasks_and_preserves_frozen_plan(self):
        room_id = self.plan()
        self.store.claim(room_id, "claude")
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now)
        self.store.recover()
        self.store.reconcile_team(room_id)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["phase"], "interrupted")
        self.assertEqual(len(snapshot["team"]["tasks"]), 2)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]))
        interrupted_work = [job for job in snapshot["jobs"] if job["kind"] == "team_work"]
        self.assertEqual(len(interrupted_work), 2)
        self.assertTrue(all(job["status"] == "interrupted" for job in interrupted_work))
        self.assertIsNone(self.store.claim(room_id, "claude"))

    def test_unknown_stops_further_team_dispatch(self):
        room_id = self.plan()
        work = self.store.claim(room_id, "claude")
        self.store.finish_job(work["id"], error="invoke_failed", uncertain=True)
        self.store.reconcile_team(room_id)
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "unknown")
        self.assertIsNone(self.store.claim(room_id, "codex"))

    def test_sibling_cleanup_preserves_first_stop_reason(self):
        room_id = self.plan()
        left, right = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.store.finish_job(left["id"], error="subscription_limit_reached")
        self.store.finish_job(right["id"], error="invoke_interrupted", uncertain=True)
        team = self.store.snapshot(room_id)["team"]
        self.assertEqual((team["phase"], team["stop_reason"]), ("stopped", "subscription_limit_reached"))
        self.store.finish_stop(room_id, confirmed=False, final_status="interrupted")
        team = self.store.snapshot(room_id)["team"]
        self.assertEqual((team["phase"], team["stop_reason"]), ("unknown", "subscription_limit_reached"))

    def test_old_stored_deadline_does_not_stop_useful_dispatch(self):
        room_id = self.plan()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET deadline=? WHERE id=?", (self.now + 1, room_id))
        self.now += 7201
        self.store.reconcile_team(room_id)
        snapshot = self.store.snapshot(room_id)
        self.assertIsNone(snapshot["team"]["stop_reason"])
        self.assertIsNotNone(self.store.claim(room_id, "claude"))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT deadline FROM rooms WHERE id=?", (room_id,)).fetchone()[0], 1001)

    def test_competing_claims_keep_one_owner_and_can_choose_other_task(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("main"), proposal("tests"), {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        self.finish(peer, [{"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        self.assertEqual(self.task(room_id, "main")["owner"], "claude")
        self.assertEqual(self.task(room_id, "tests")["status"], "open")
        coord = self.store.claim(room_id, "codex")
        self.finish(coord, [{"type": "claim", "task_id": "tests"}, {"type": "plan_ready"}])
        # The plan author is also asked once to coordinate the initially open
        # task; ending that already queued call does not duplicate work.
        author_coord = self.store.claim(room_id, "claude")
        if author_coord and author_coord["kind"] == "team_coordinate":
            self.finish(author_coord, [])
        self.assertEqual(self.task(room_id, "tests")["owner"], "codex")
        self.assertEqual(self.store.snapshot(room_id)["team"]["phase"], "working")

    def test_ready_author_with_owned_work_is_not_asked_for_empty_coordination(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("main"), proposal("tests"),
                             {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        self.finish(peer, [])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual([(job["provider"], job["kind"]) for job in snapshot["jobs"] if job["status"] == "queued"],
                         [("codex", "team_coordinate")])
        self.assertIsNone(self.store.claim(room_id, "claude"))
        self.finish(self.store.claim(room_id, "codex"), [{"type": "claim", "task_id": "tests"}, {"type": "plan_ready"}])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(len(snapshot["jobs"]), 5)  # 2 planning + 1 necessary confirmation + 2 independent work calls.
        self.assertFalse(any(job["kind"] == "team_coordinate" and job["provider"] == "claude" for job in snapshot["jobs"]))
        self.assertEqual({job["provider"] for job in snapshot["jobs"] if job["status"] == "queued"}, {"claude", "codex"})

    def test_user_supplement_is_idempotent_and_does_not_queue_busy_member(self):
        room_id = self.plan()
        before = len(self.store.snapshot(room_id)["jobs"])
        self.assertTrue(self.store.send_message(room_id, "Clarification while both tasks are queued", "all", "clarification"))
        self.assertFalse(self.store.send_message(room_id, "Clarification while both tasks are queued", "all", "clarification"))
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(len(snapshot["jobs"]), before)
        self.assertEqual(sum(event["kind"] == "message" and event.get("text") == "Clarification while both tasks are queued" for event in snapshot["events"]), 1)

    def test_user_supplement_can_explicitly_resume_blocked_owned_task(self):
        room_id = self.plan(dependency=True)
        self.finish(self.store.claim(room_id, "claude"), [{"type": "block", "task_id": "main", "reason": "Need the missing requirement."}])
        self.assertTrue(self.store.send_message(room_id, "The requirement is now explicit.", "claude", "resume-message"))
        job = self.store.claim(room_id, "claude")
        self.assertEqual(job["kind"], "team_user")
        self.finish(job, [{"type": "claim", "task_id": "main"}])
        self.assertEqual(self.store.claim(room_id, "claude")["kind"], "team_work")

    def test_old_stored_call_budget_does_not_block_required_review(self):
        room_id = self.plan(dependency=True)
        job = self.store.claim(room_id, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET used=32,call_limit=32 WHERE id=?", (room_id,))
        self.finish(job, [{"type": "complete", "task_id": "main", "result": "Awaiting review"}])
        snapshot = self.store.snapshot(room_id)
        self.assertIsNone(snapshot["team"]["stop_reason"])
        self.assertNotEqual(snapshot["room"]["status"], "ready_for_user")
        self.assertIsNotNone(self.store.claim(room_id, "codex"))
        self.assertTrue(self.store.send_message(room_id, "Continue the authorized task", "claude", "after-old-budget"))

    def test_simple_results_can_progress_past_eight_calls_but_unchanged_peer_loop_stops_and_user_update_resets(self):
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now,
                               agents={"claude": "Claude", "codex": "Codex", "helper": "Helper"})
        room_id, _ = self.store.create_room("Write a concrete answer", "claude", "simple-progress")
        self.store.send_message(room_id, "Produce an independent result", "helper", "independent-result")
        held = self.store.claim(room_id, "helper")
        provider = "claude"
        for index in range(12):
            job = self.store.claim(room_id, provider)
            self.assertIsNotNone(job)
            peer = "codex" if provider == "claude" else "claude"
            self.store.finish_job(job["id"], message=f"Concrete answer revision {index}",
                                  action={"to": peer, "kind": "help", "text": "Check the current answer"})
            provider = peer
        self.assertEqual(self.store.snapshot(room_id)["budget"]["used"], 13)
        for index in range(10):
            job = self.store.claim(room_id, provider)
            if job is None:
                break
            peer = "codex" if provider == "claude" else "claude"
            self.store.finish_job(job["id"], message="No new finding.",
                action={"to": peer, "kind": "help", "text": f"Reworded request {index}"})
            provider = peer
        in_flight = next(job for job in self.store.snapshot(room_id)["jobs"] if job["id"] == held["id"])
        self.assertEqual(in_flight["status"], "working")
        self.store.finish_job(held["id"], message="The independent result is complete.")
        blocked = self.store.snapshot(room_id)
        self.assertEqual(next(job for job in blocked["jobs"] if job["id"] == held["id"])["status"], "completed")
        self.assertTrue(any(event.get("job_id") == held["id"] and event.get("text") == "The independent result is complete." for event in blocked["events"]))
        self.assertEqual(blocked["room"]["status"], "needs_user")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT blocked_reason FROM rooms WHERE id=?", (room_id,)).fetchone()[0], "no_progress")
        self.assertTrue(any(event.get("code") == "no_progress" for event in blocked["events"]))
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in blocked["jobs"]))
        self.assertTrue(self.store.send_message(room_id, "New requirement: check this specific missing premise.", "claude", "simple-new-requirement"))
        fresh = self.store.claim(room_id, "claude")
        self.assertIsNotNone(fresh)
        self.store.finish_job(fresh["id"], message="No new finding.",
                              action={"to": "codex", "kind": "help", "text": "Check the new premise"})
        self.assertIsNotNone(self.store.claim(room_id, "codex"))

    def test_multiple_same_owner_tasks_are_serialized_and_paths_require_dependencies(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("one", paths=["main.py"]), proposal("two", dependencies=["one"], paths=["main.py"]),
                             {"type": "claim", "task_id": "one"}, {"type": "claim", "task_id": "two"}, {"type": "plan_ready"}])
        self.finish(peer, [])
        self.finish(self.store.claim(room_id, "codex"), [{"type": "plan_ready"}])
        self.assertEqual(self.store.claim(room_id, "claude")["team"]["task_id"], "one")
        self.assertIsNone(self.store.claim(room_id, "claude"))
        self.assertIsNone(self.store.claim(room_id, "codex"))

    def test_review_queue_is_not_duplicated_across_three_independent_members(self):
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now, agents={"claude": "Claude", "codex": "Codex", "helper": "Helper"})
        room_id = self.room()
        plans = {provider: self.store.claim(room_id, provider) for provider in ("claude", "codex", "helper")}
        self.finish(plans["claude"], [proposal("one"), {"type": "claim", "task_id": "one"}, {"type": "plan_ready"}])
        for provider in ("codex", "helper"):
            self.finish(plans[provider], [])
        for provider in ("codex", "helper"):
            self.finish(self.store.claim(room_id, provider), [{"type": "plan_ready"}])
        work = self.store.claim(room_id, "claude")
        self.finish(work, [{"type": "complete", "task_id": "one", "result": "Result"}])
        for _ in range(3):
            self.store.reconcile_team(room_id)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(sum(job["kind"] == "team_review" for job in snapshot["jobs"]), 1)

    def test_team_sandbox_execution_records_observation_without_legacy_feedback(self):
        room_id = self.store.create_room("Sandbox goal", "claude", "sandbox-team", collaboration=True, mode="python_sandbox")[0]
        # This fixture starts at the accepted task boundary; runner integration
        # and cleanup are owned by service tests, not fabricated by this test.
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='completed' WHERE room_id=?", (room_id,))
            db.execute("UPDATE teams SET phase='ready_for_user' WHERE room_id=?", (room_id,))
            db.execute("UPDATE rooms SET status='ready_for_user' WHERE id=?", (room_id,))
        reservation = self.store.reserve_execution(room_id, 1, "a" * 64)
        self.assertIsNotNone(reservation)
        self.assertTrue(self.store.claim_execution(reservation["id"]))
        self.store.finish_execution(reservation["id"], {"cleanup_confirmed": True, "status": "completed", "exit_code": 0})
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "ready_for_user")
        self.assertFalse(any(job["kind"] == "execution" for job in snapshot["jobs"]))
        second = self.store.reserve_execution(room_id, 2, "b" * 64)
        self.store.claim_execution(second["id"])
        self.store.finish_execution(second["id"], {"cleanup_confirmed": True, "status": "completed", "exit_code": 1})
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["stop_reason"], "execution_failed")
        self.assertEqual(snapshot["room"]["status"], "needs_user")

    def test_unavailable_task_capability_never_starts_work(self):
        room_id = self.room()
        author, peer = self.store.claim(room_id, "claude"), self.store.claim(room_id, "codex")
        self.finish(author, [proposal("art", capability="image"), {"type": "plan_ready"}])
        self.finish(peer, [])
        self.finish(self.store.claim(room_id, "codex"), [{"type": "plan_ready"}])
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(self.task(room_id, "art")["blocked_reason"], "capability_unavailable")
        self.assertFalse(any(job["kind"] == "team_work" for job in snapshot["jobs"]))
        self.assertEqual(snapshot["room"]["status"], "needs_user")


if __name__ == "__main__":
    unittest.main()
