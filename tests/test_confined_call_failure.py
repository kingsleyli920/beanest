"""One native call ends abnormally while other partners are still working.

Only that call's task is set back and handed out again; the others finish and
their results are accepted. Whatever the host cannot account for keeps the
whole-room stop. Fictional project and partners on real SQLite, real private
trees and the real job loop; only the native transport is synthetic.
"""
import asyncio
import json

import test_task_pool as pool

plan = pool.plan


class ConfinedCallFailure(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    def fail_calls(self, phase, task_id, *, times=1, change=None, before=None):
        """End the first `times` calls for this step the way a CLI that exited with an error does."""
        inner = self.service.native_invoke
        self.failed = []

        async def invoke(agent, prompt, cwd, **options):
            result = await inner(agent, prompt, cwd, **options)
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            if (context["job"]["job_phase"], (context["task"] or {}).get("id")) != (phase, task_id) or len(self.failed) >= times:
                return result
            self.failed.append(context["job"]["id"])
            if before is not None:
                before(cwd)
            failed = {**result, "status": "failed", "failure_code": "cli_nonzero_exit", "text": ""}
            return change(failed) if change else failed
        self.service.native_invoke = invoke
        if task_id == "T1" and phase == "work":
            # The call that fails is still running when the other partner's call has begun.
            self.holds["T1"] = self.started("T2", 1)

    def started(self, task_id, count):
        async def condition():
            return sum(1 for _, worked in self.works if worked == task_id) >= count
        return condition

    @staticmethod
    async def never():
        return False

    def kinds(self, state, kind):
        return [event for event in state["events"] if event["kind"] == kind]

    def work_jobs(self, state, task_id):
        """(status, error) of every work call made for a task, oldest first."""
        created = {event["job_id"]: event["text"] for event in self.kinds(state, "job_created") if event["job_kind"] == "team_work"}
        return [(job["status"], job["error"]) for job in state["jobs"] if f"Task {task_id}:" in created.get(job["id"], "")]

    async def test_the_other_call_runs_on_and_the_failed_task_is_done_again(self):
        # The reported case: one partner's call exits with an error while the other is mid-task.
        # Catches the whole room stopping for it (the other call cancelled as invoke_interrupted and
        # a new run started), the other partner's result being refused, and the failed task being
        # left where nobody picks it up.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1")
        self.holds["T2"] = self.started("T1", 2)   # codex stays mid-call until claude's task is handed out again
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        confined = self.kinds(state, "call_failure_confined")
        self.assertEqual([(event["provider"], event["task_id"], event["phase"], event["code"], event["attempt"],
                           event["old_commands_accepted"]) for event in confined],
                         [("claude", "T1", "work", "cli_nonzero_exit", 1, False)])
        peer = next(job for job in state["jobs"] if job["provider"] == "codex" and job["id"] in confined[0]["running_job_ids"])
        self.assertEqual((peer["status"], peer["error"]), ("completed", None), "the call that was running finished normally")
        self.assertEqual(self.kinds(state, "objective_stopped"), [])
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], ["cli_nonzero_exit"])
        self.assertEqual(self.work_jobs(state, "T1"), [("failed", "cli_nonzero_exit"), ("completed", None)])
        self.assertEqual(self.work_jobs(state, "T2"), [("completed", None)])
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("claude", "T1"), ("codex", "T2")])
        self.assertEqual({task["id"]: (task["status"], task["owner"]) for task in state["team"]["tasks"]},
                         {"T1": ("accepted", "claude"), "T2": ("accepted", "codex")})
        # Only this call's own warning: planning may leave unrelated ones, depending on who answered first.
        self.assertEqual([(event["code"], event.get("confined")) for event in self.kinds(state, "warning")
                          if event.get("job_id") == confined[0]["job_id"]],
                         [("cli_nonzero_exit", True)], "the page is told this failure asks nothing of the user")
        self.assertEqual(state["current_run"]["number"], 1, "no new run was started")
        self.assertEqual(len(self.kinds(state, "run_started")), 0)
        self.assertEqual([member["status"] for member in state["members"]], ["idle", "idle"])

    async def test_the_owner_is_told_that_its_previous_call_ended(self):
        # Catches the task coming back as if nothing had happened: the owner would not know that
        # nothing of its earlier call was accepted, or that the files it left are still there.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1")
        self.holds["T2"] = self.started("T1", 2)
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        texts = [event["text"] for event in self.kinds(state, "job_created")
                 if event["job_kind"] == "team_work" and "Task T1:" in event["text"]]
        self.assertEqual(len(texts), 2)
        note = "The call before this one for this step ended before it replied (cli_nonzero_exit)"
        self.assertNotIn(note, texts[0])
        self.assertIn(note, texts[1])
        self.assertIn("is still in your working copy", texts[1])
        other = [event["text"] for event in self.kinds(state, "job_created") if "Task T2:" in event["text"]]
        self.assertFalse(any(note in text for text in other), "only the failed step carries the note")

    async def test_a_review_that_dies_is_asked_again_without_touching_the_work_in_progress(self):
        # Catches confinement that only knows work calls: a reviewer's dead call would still cancel
        # the partner who is in the middle of another task.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("review", "T1")
        async def until_t1_accepted():
            return (await self.statuses()).get("T1") == "accepted"
        self.holds["T2"] = until_t1_accepted
        await self.create(["claude", "codex", "third"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual([(event["task_id"], event["phase"], event["attempt"]) for event in self.kinds(state, "call_failure_confined")],
                         [("T1", "review", 1)])
        self.assertEqual(self.kinds(state, "objective_stopped"), [])
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], ["cli_nonzero_exit"])
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2")], "no work was repeated")

    async def test_a_help_answer_that_dies_is_asked_again_without_touching_the_work_in_progress(self):
        # Catches a dead helper leaving the asker waiting for ever, or cancelling the partner who is
        # busy with another task: the request must reach the helper a second time, unchanged.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("help", "T1")
        async def until_t1_accepted():
            return (await self.statuses()).get("T1") == "accepted"
        self.holds["T2"] = until_t1_accepted
        base, asked, questions = self.fake.handler, [], []
        async def handler(agent, context, cwd):
            phase, task = context["job"]["job_phase"], context["task"]
            if phase == "work" and task["id"] == "T1" and not asked:
                asked.append(True)
                for _ in range(500):
                    if ("codex", "T2") in self.works:
                        break
                    await asyncio.sleep(0.01)
                return pool.native_reply(context, [{"type": "request_help", "task_id": "T1", "to": "third",
                                                    "capability": "text", "text": "Which value should T1 keep?"}])
            if phase == "help":
                questions.append((context["self"], task["help_text"]))
                return pool.native_reply(context)
            return await base(agent, context, cwd)
        self.fake.handler = handler
        await self.create(["claude", "codex", "third"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual([(event["provider"], event["task_id"], event["phase"], event["attempt"])
                          for event in self.kinds(state, "call_failure_confined")], [("third", "T1", "help", 1)])
        self.assertEqual(questions, [("third", "Which value should T1 keep?")] * 2)
        self.assertEqual(self.kinds(state, "objective_stopped"), [])
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], ["cli_nonzero_exit"])
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2")])

    async def test_unconfirmed_cleanup_still_stops_every_call(self):
        # Catches confinement reaching a call whose processes may still be running: its files could
        # change under the partners that keep working.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1", change=lambda result: {**result, "cleanup_confirmed": False,
                  "native_execution": {**result["native_execution"], "cleanup_confirmed": False}})
        self.holds["T2"] = self.never
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "native_cleanup_unconfirmed"))
        self.assertEqual(self.kinds(state, "call_failure_confined"), [])
        self.assertEqual(sorted((job["provider"], job["error"]) for job in state["jobs"] if job["error"]),
                         [("claude", "native_cleanup_unconfirmed"), ("codex", "invoke_interrupted")])
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2")], "nothing was handed out again")

    async def test_a_new_file_nobody_holds_goes_to_the_task_when_it_is_done_again(self):
        # Catches the audit being stricter for a failed call than for a successful one: a work call
        # may touch a file no unfinished task holds, and the host adds it to the task on submission.
        # Stopping the room for it would cut off the other partners for an ordinary way of working.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1", before=lambda cwd: (cwd / "helper.py").write_text("# written by the call that then died\n"))
        self.holds["T2"] = self.started("T1", 2)
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual([event["attempt"] for event in self.kinds(state, "call_failure_confined")], [1])
        self.assertEqual(self.kinds(state, "objective_stopped"), [])
        paths = {task["id"]: task["write_paths"] for task in state["team"]["tasks"]}
        self.assertEqual(paths, {"T1": ["T1.py", "helper.py"], "T2": ["T2.py"]})
        published = {file["path"]: file["content"] for file in self.service._project(self.room_id).snapshot()["files"]}
        self.assertEqual(published["helper.py"], "# written by the call that then died\n")

    async def test_a_review_that_changed_a_file_still_stops_every_call(self):
        # Catches the allowance for a work call's own files leaking to calls that may write nothing.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("review", "T1", before=lambda cwd: (cwd / "helper.py").write_text("# a reviewer wrote this\n"))
        self.holds["T2"] = self.never
        await self.create(["claude", "codex", "third"])
        await self.settle()
        state = await self.state()
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "cli_nonzero_exit"))
        self.assertEqual(self.kinds(state, "call_failure_confined"), [])
        self.assertIn(("codex", "invoke_interrupted"), [(job["provider"], job["error"]) for job in state["jobs"]])

    async def test_a_change_to_a_file_another_unfinished_task_holds_still_stops_every_call(self):
        # Catches confinement without the audit of the private tree: a failed call that touched a
        # file another partner is working on has an effect nobody has accounted for.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1", before=lambda cwd: (cwd / "T2.py").write_text("# changed by the call that then died\n"))
        self.holds["T2"] = self.never
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "cli_nonzero_exit"))
        self.assertEqual(self.kinds(state, "call_failure_confined"), [])
        self.assertEqual(sorted((job["provider"], job["status"], job["error"]) for job in state["jobs"] if job["error"]),
                         [("claude", "unknown", "cli_nonzero_exit"), ("codex", "unknown", "invoke_interrupted")])

    async def test_a_tool_that_never_returned_still_stops_every_call(self):
        # Catches confinement of a call that ended in the middle of a tool: what that tool did is unknown.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1", change=lambda result: {**result,
                  "native_execution": {**result["native_execution"], "tool_calls": 2, "tool_results": 1}})
        self.holds["T2"] = self.never
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "cli_nonzero_exit"))
        self.assertEqual(self.kinds(state, "call_failure_confined"), [])
        self.assertIn(("codex", "invoke_interrupted"), [(job["provider"], job["error"]) for job in state["jobs"]])

    async def test_a_task_that_keeps_failing_stops_the_room_at_the_third_failure(self):
        # Catches an endless loop of calls on a task whose call dies every time: after two confined
        # failures the room stops as before, where the host's continuation pauses longer each time.
        self.plan = [plan("T1"), plan("T2")]
        self.fail_calls("work", "T1", times=99)
        self.holds["T2"] = self.never
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual([event["attempt"] for event in self.kinds(state, "call_failure_confined")], [1, 2])
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "cli_nonzero_exit"))
        own = [event.get("confined") for event in self.kinds(state, "warning") if event["code"] == "cli_nonzero_exit"]
        self.assertEqual(own, [True, True, None], "the failure that stopped the room is a warning the user sees")
        self.assertEqual(self.work_jobs(state, "T1"),
                         [("failed", "cli_nonzero_exit"), ("failed", "cli_nonzero_exit"), ("unknown", "cli_nonzero_exit")])
        self.assertEqual(self.work_jobs(state, "T2"), [("unknown", "invoke_interrupted")])
        self.assertEqual(len(self.failed), 3)

    async def test_with_no_other_call_running_the_room_stops_as_before(self):
        # Catches confinement taking over the case where nobody is cut off: that stop has its own
        # recoveries (the retained partial work and its read-only inspection), which must still apply.
        self.plan = [plan("T1")]
        self.fail_calls("work", "T1")
        del self.holds["T1"]
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual((state["team"]["phase"], state["team"]["stop_reason"]), ("unknown", "cli_nonzero_exit"))
        self.assertEqual(self.kinds(state, "call_failure_confined"), [])
        self.assertEqual(self.works, [("claude", "T1")])


if __name__ == "__main__":
    import unittest
    unittest.main()
