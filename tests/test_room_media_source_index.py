"""Published-media provenance lookup scales with events, without changing trust."""
from types import SimpleNamespace
import unittest

from local_agent_coordinator.room_service import RoomService


class CountedEvents(list):
    def __init__(self, values):
        super().__init__(values)
        self.forward_passes = self.reverse_passes = 0

    def __iter__(self):
        self.forward_passes += 1
        return super().__iter__()

    def __reversed__(self):
        self.reverse_passes += 1
        return super().__reversed__()


def source_fixture(task_count=971, event_count=112169):
    tasks, events, jobs, executions, roles, checkpoints = [], [], [], [], {}, []
    def event(kind, **fields):
        events.append({"seq": len(events) + 1, "kind": kind, **fields})
    for number in range(task_count):
        run, identifier = "run-" + str(number // 6), "T" + str(number % 6)
        task = {"run_id": run, "id": identifier, "result": "Published " + identifier,
            "result_version": 2, "assignment_epoch": 3, "owner": "codex", "status": "accepted",
            "inherited_from_run": "run-" + str(number // 6 - 1) if number >= 6 else None}
        tasks.append(task)
        work, review = "work-" + str(number), "review-" + str(number)
        roles[work] = {"run_id": run, "phase": "work", "result_version": 1, "assignment_epoch": 3}
        roles[review] = {"run_id": run, "phase": "review", "result_version": 2, "assignment_epoch": 3}
        jobs.append({"id": work, "status": "completed"})
        executions.append({"job_id": work, "status": "completed"})
        event("team_command", command="complete", run_id=run, task_id=identifier, sender="codex", job_id=work)
        event("team_command", command="review", run_id=run, task_id=identifier, sender="claude", job_id=review)
        if number % 6 == 5 or number == task_count - 1:
            checkpoint = "checkpoint-" + str(number // 6)
            roles[checkpoint] = {"run_id": run, "phase": "checkpoint"}
            jobs.append({"id": checkpoint, "status": "completed"})
            executions.append({"job_id": checkpoint, "status": "completed"})
            event("job_finished", job_id=checkpoint)
            event("objective_checkpoint_finished", decision="complete", code=None,
                  objective_id="objective", run_id=run)
            checkpoints.append({"objective_id": "objective", "run_id": run, "summary": "Checked " + run})
    while len(events) < event_count:
        event("native_tool_activity", job_id="current")
    snapshot = {"room": {"mode": "native_project"}, "current_run": {"id": tasks[-1]["run_id"]},
        "objective": {"id": "objective", "next_step": {}}, "team": {"tasks": []},
        "members": [], "parallel_sessions": [], "events": CountedEvents(events),
        "jobs": jobs, "native_executions": executions}
    snapshot["team"]["tasks"] = [task for task in tasks if task["run_id"] == snapshot["current_run"]["id"]]
    service = RoomService.__new__(RoomService)
    service.store = SimpleNamespace(job_roles=lambda room: roles,
        media_history=lambda room: {"tasks": tasks, "checkpoints": checkpoints})
    service._project = lambda room: SimpleNamespace(snapshot=lambda: {"files": [], "source": None})
    return service, snapshot, tasks, roles


class RoomMediaSourceIndexTests(unittest.TestCase):
    def test_timeline_is_not_rescanned_for_every_inherited_task_and_checkpoint(self):
        service, snapshot, tasks, _ = source_fixture(task_count=72, event_count=2000)
        _, texts, roots = service._room_media_sources("room", snapshot=snapshot, include_documents=False)
        self.assertLessEqual(snapshot["events"].forward_passes, 3)
        self.assertEqual(snapshot["events"].reverse_passes, 0)
        self.assertEqual(roots, [])
        # Six carried task results plus one checkpoint per historical run.
        self.assertEqual(len(texts), 18)
        reports = [item for item in texts if "task_id" in item[1]]
        self.assertTrue(all(source["run_id"] == "run-0" and external for _, source, external in reports))

    def test_review_epoch_and_native_owner_version_status_still_bound(self):
        service, snapshot, tasks, roles = source_fixture(task_count=6, event_count=30)
        # Without checkpoint evidence, only the exact review version/epoch
        # can mark the task as delivered.
        snapshot["events"] = CountedEvents(event for event in snapshot["events"]
                                          if event["kind"] != "objective_checkpoint_finished")
        roles["review-0"]["assignment_epoch"] = 4
        snapshot["jobs"][0]["status"] = "failed"
        roles["work-1"]["result_version"] = 2
        snapshot["events"][4]["sender"] = "claude"
        snapshot["native_executions"][3]["status"] = "failed"
        tasks[4]["status"] = "withdrawn"
        _, texts, _ = service._room_media_sources("room", snapshot=snapshot, include_documents=False)
        reports = {source["task_id"]: (source, external) for _, source, external in texts if "task_id" in source}
        self.assertEqual(set(reports), {"T0", "T1", "T2", "T3", "T5"})
        self.assertNotIn("delivery", reports["T0"][0])
        self.assertIn("delivery", reports["T1"][0])
        self.assertEqual({task: external for task, (_, external) in reports.items()},
                         {"T0": False, "T1": False, "T2": False, "T3": False, "T5": True})
