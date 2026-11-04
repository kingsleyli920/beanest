"""Real loopback API gates while a same-run repair is in progress."""
import asyncio
import threading
import unittest

from tests import test_team_runs_http as http
from tests import test_team_service as fixtures
from tests import test_room_execution as execution


class TeamExecutionRepairHTTPTests(unittest.TestCase):
    request = http.TeamRunHTTPTests.request
    ready = http.TeamRunHTTPTests.ready

    def setUp(self):
        http.TeamRunHTTPTests.setUp(self)
        self.release_repair = threading.Event()
        self.repair_started = threading.Event()
        self.runner = execution.FakeRunner()
        self.service._runner_instance = self.runner
        self.contents = {"main":"VALUE = 1\n", "checks":"EXPECTED = 1\n"}
        async def work(agent, context, count):
            if context["job"]["repair_execution_id"]:
                self.assertEqual(context["execution_observation"]["result"]["report_trust"], "untrusted")
                self.repair_started.set()
                while not self.release_repair.is_set():
                    await asyncio.sleep(.001)
                self.contents[context["task"]["id"]] = "VALUE = 2\n" if context["task"]["id"] == "main" else "EXPECTED = 2\n"
            return None
        self.provider.handler = fixtures.TeamScenario(self,
            [fixtures.task("main","main.py"), fixtures.task("checks","checks.py")],
            {"main":"claude", "checks":"codex"}, self.contents, work=work)
        self.runner.handler = lambda call:{"status":"failed" if len(self.runner.calls) == 1 else "completed",
            "exit_code":1 if len(self.runner.calls) == 1 else 0, "cleanup_confirmed":True, "reason":None}

    def tearDown(self):
        self.release_repair.set()
        http.TeamRunHTTPTests.tearDown(self)

    def test_accept_is_blocked_during_repair_and_binds_the_new_exact_result(self):
        code,created = self.request("POST","/api/rooms",{"goal":"Repair a known bounded execution failure", "target":"all",
            "request_id":"repair-http", "mode":"python_sandbox", "collaboration":True})
        self.assertEqual(code,200)
        room = created["room"]["id"]
        self.assertTrue(self.repair_started.wait(2))
        code,working = self.request("GET","/api/rooms/"+room)
        self.assertEqual(code,200)
        current = working["current_run"]
        first_id = working["executions"][0]["id"]
        self.assertEqual(working["team"]["execution_repair_id"],first_id)
        self.assertFalse(current["can_accept"])
        old = {"run_id":current["id"], "revision":current["revision"], "digest":current["digest"]}
        code,error = self.request("POST",f"/api/rooms/{room}/accept",{"request_id":"early", **old})
        self.assertEqual((code,error),(409,{"error":"team_run_not_ready"}))
        self.release_repair.set()
        finished = self.ready(room)
        self.assertEqual(len(finished["executions"]),2)
        self.assertEqual(finished["current_run"]["id"],current["id"])
        self.assertEqual(len(finished["runs"]),1)
        self.assertEqual(finished["executions"][0]["status"],"failed")
        self.assertNotEqual(finished["current_run"]["digest"],current["digest"])
        code,error = self.request("POST",f"/api/rooms/{room}/accept",{"request_id":"old-version", **old})
        self.assertEqual((code,error),(409,{"error":"team_run_conflict"}))
        current = finished["current_run"]
        code,accepted = self.request("POST",f"/api/rooms/{room}/accept",{"request_id":"accept-fixed", "run_id":current["id"],
            "revision":current["revision"], "digest":current["digest"]})
        self.assertEqual(code,200)
        self.assertIsNotNone(accepted["current_run"]["accepted_at"])
        self.assertEqual(len(self.runner.calls),2)


if __name__ == "__main__":
    unittest.main()
