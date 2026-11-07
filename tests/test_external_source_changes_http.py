"""Work done outside a room reaches its partners over real HTTP, SQLite and Git."""
import os
import subprocess
import time
import unittest

from local_agent_coordinator import room_service
import test_team_runs_http as http_fixtures
import test_native_room_service as native_fixtures


class ExternalSourceChangesHTTPTests(unittest.TestCase):
    setUp = http_fixtures.TeamRunHTTPTests.setUp
    tearDown = http_fixtures.TeamRunHTTPTests.tearDown
    request = http_fixtures.TeamRunHTTPTests.request
    handler = native_fixtures.NativeRoomServiceTests.handler

    def git(self, *arguments, when=None):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                           GIT_CONFIG_NOSYSTEM="1", HOME=str(self.root))
        if when is not None:
            environment.update(GIT_AUTHOR_DATE=f"@{int(when)} +0000", GIT_COMMITTER_DATE=f"@{int(when)} +0000")
        return subprocess.run(["git", *arguments], cwd=self.source, env=environment, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name, text, subject, *, when=None):
        (self.source / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", subject, when=when)
        return self.git("rev-parse", "HEAD")

    def configure(self):
        self.source = self.root / "native-source"
        self.source.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.commit("main.py", "VALUE = 1\n", "Fictional baseline")
        self.native = native_fixtures.NativeTransport()
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        code, value = self.request("POST", "/api/projects", {"name": "Native example", "path": str(self.source),
                                                             "selection_kind": "folder"})
        self.assertEqual(code, 200, value)
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Update main.py.", "target": "claude", "request_id": "external-source-create",
            "mode": "native_project", "project_id": value["project"]["id"], "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        return created["room"]["id"]

    def wait(self, room_id, predicate):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            code, result = self.request("GET", "/api/rooms/" + room_id)
            self.assertEqual(code, 200)
            if predicate(result):
                return result
            time.sleep(0.005)
        self.fail("Native HTTP room did not settle")

    def follow_up(self, room_id, before, text):
        count = len(self.native.calls)
        code, started = self.request("POST", f"/api/rooms/{room_id}/messages", {
            "text": text, "target": "all", "request_id": "follow-up-" + str(count), "intent": "continue",
            "binding": before["continuation"]["binding"]})
        self.assertEqual(code, 200, started)
        final = self.wait(room_id, lambda value: value["objective"]["status"] == "complete"
                          and value["objective"]["id"] != before["objective"]["id"]
                          and value["current_run"]["can_reassess_objective"])
        return final, self.native.calls[count:]

    def test_commits_made_while_the_room_was_idle_reach_every_later_prompt_once(self):
        # Catches: a room resumed after the user finished its work elsewhere,
        # and its partners reviewed/redid a stale board with no hint of it.
        room_id = self.configure()
        first = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        self.assertTrue(all("external_source_changes" not in call["context"] for call in self.native.calls))
        self.assertEqual(sum(event["kind"] == "source_advanced_externally" for event in first["events"]), 0)

        later = time.time() + 120  # Clearly outside every job window of this room.
        head = self.commit("main.py", "VALUE = 2\n", "Fix main.py outside the room", when=later)
        final, calls = self.follow_up(room_id, first, "Continue with the remaining work.")
        self.assertTrue(calls)
        for call in calls:
            changes = call["context"]["external_source_changes"]
            self.assertEqual(len(changes), 1, call["context"]["job"])
            self.assertEqual(changes[0]["to_head"], head)
            self.assertEqual([commit["subject"] for commit in changes[0]["commits"]], ["Fix main.py outside the room"])
            self.assertEqual((changes[0]["external_count"], changes[0]["complete"]), (1, True))
            self.assertIn(room_service.EXTERNAL_SOURCE_INSTRUCTION, call["prompt"])
        recorded = [event for event in final["events"] if event["kind"] == "source_advanced_externally"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual((recorded[0]["sender"], recorded[0]["repository"]), ("system", "native-source"))

        # Nothing new happened outside: the next round carries no notice.
        again, calls = self.follow_up(room_id, final, "One more check.")
        self.assertTrue(calls)
        self.assertTrue(all("external_source_changes" not in call["context"] for call in calls))
        self.assertEqual(sum(event["kind"] == "source_advanced_externally" for event in again["events"]), 1)

    def test_a_partner_commit_during_a_job_is_not_reported_as_outside_work(self):
        # Catches: attributing the room's own commits to someone else, which
        # would tell partners their just-finished work came from outside.
        room_id = self.configure()
        first = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        handler = self.native.handler

        def committing(agent, context, cwd):
            if not getattr(self, "_committed", False):
                self._committed = True
                self.commit("main.py", "VALUE = 3\n", "Partner commit during its job")
            return handler(agent, context, cwd)

        self.native.handler = committing
        final, calls = self.follow_up(room_id, first, "Continue and commit.")
        self.assertTrue(getattr(self, "_committed", False), "fixture never reached a job")
        self.native.handler = handler
        again, calls = self.follow_up(room_id, final, "Check again.")
        self.assertTrue(all("external_source_changes" not in call["context"] for call in calls))
        self.assertEqual(sum(event["kind"] == "source_advanced_externally" for event in again["events"]), 0)

    def test_an_existing_room_without_an_observation_starts_from_its_last_finished_job(self):
        # Catches: the first observation after upgrading silently adopting
        # everything done while the room sat stopped as its starting point.
        room_id = self.configure()
        first = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        with self.service.store._connection(write=True) as db:
            db.execute("DELETE FROM room_source_observations WHERE room_id=?", (room_id,))
        self.commit("main.py", "VALUE = 4\n", "Done elsewhere while stopped", when=time.time() + 120)
        _, calls = self.follow_up(room_id, first, "Continue.")
        self.assertEqual([commit["subject"] for commit in calls[0]["context"]["external_source_changes"][0]["commits"]],
                         ["Done elsewhere while stopped"])

    def test_an_interrupted_job_without_job_finished_does_not_hide_later_outside_work(self):
        # Catches: a job interrupted at shutdown (claim + job_cleanup, no
        # job_finished) keeping its window open forever and swallowing every
        # later outside commit, as in the real 1a10086d room.
        room_id = self.configure()
        self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        snapshot = self.service.store.snapshot(room_id)
        start = time.time() + 30
        snapshot["events"] += [
            {"seq": 10**9, "kind": "job_claimed", "job_id": "interrupted-job", "created_at": start},
            {"seq": 10**9 + 1, "kind": "job_cleanup", "job_id": "interrupted-job", "created_at": start + 2}]
        self.commit("main.py", "VALUE = 5\n", "Done after the interrupted job", when=start + 90)
        self.service.clock = lambda: start + 120
        job = {"room_id": room_id, "id": "probe-job", "team": {"run_id": snapshot["current_run"]["id"]}}
        changes = self.service._external_source_changes(job, snapshot)
        self.assertEqual([commit["subject"] for commit in changes[-1]["commits"]], ["Done after the interrupted job"])

    def test_a_new_room_of_a_non_git_project_watches_the_repository_chosen_before(self):
        # Catches: 九烛 registers a trimmed non-Git copy; a new room had no
        # binding of its own, so outside work was never observed there.
        from local_agent_coordinator import git_status
        self.configure()
        copy = self.root / "trimmed-copy"
        copy.mkdir()
        (copy / "README.md").write_text("Fictional trimmed copy.\n")
        code, value = self.request("POST", "/api/projects", {"name": "Trimmed copy", "path": str(copy),
                                                             "selection_kind": "folder"})
        self.assertEqual(code, 200, value)
        project_id = value["project"]["id"]
        rooms = [self.service.store.create_room(goal, "claude", goal, mode="native_project", project_id=project_id,
                                                participants=["claude", "codex"])[0]
                 for goal in ("Earlier fictional room", "Later fictional room")]
        self.assertIsNone(self.service._source_repository(rooms[1], project_id))
        identity = git_status.repository_identity(self.source)
        self.service.store.bind_delivery_repository(rooms[0], {"workspace_id": "delivery:fixture",
            "path": str(self.source), "label": "交付所在仓库 · native-source", "identity": identity})
        self.assertEqual(self.service._source_repository(rooms[1], project_id), identity["root"])

    def test_a_missing_repository_never_blocks_the_job(self):
        # Catches: turning an observation failure into a stopped room.
        room_id = self.configure()
        first = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        (self.source / ".git").rename(self.root / "moved-git")
        final, calls = self.follow_up(room_id, first, "Continue without Git.")
        self.assertTrue(calls)
        self.assertTrue(all("external_source_changes" not in call["context"] for call in calls))
        self.assertEqual(final["objective"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
