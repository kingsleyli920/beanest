"""A call cut off by a network outage goes on in its own session once the network is back.

Fictional project, real SQLite and the real job loop; the native transport and
this machine's network state are synthetic (see test_task_pool).
"""
import asyncio
import json
from unittest.mock import patch

from local_agent_coordinator import network_state, room_service
import test_task_pool as pool

plan = pool.plan
ERROR_WORDS = "API Error: Can't reach the API server - check your internet or DNS (ENOTFOUND)"


class ConnectionOutage(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name, value in (("_CONNECTION_BACKOFF_SECONDS", (0.01,) * 6), ("_CONNECTION_POLL_SECONDS", 0.01)):
            patcher = patch.object(room_service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.routes = []          # what each look at the network finds, in order; the last answer repeats
        self.looks = 0
        self.cuts = 1             # how many work calls the outage ends
        self.last_words = None    # what the cut-off call said in public before it ended
        self.cut_sessions = []
        self.work_calls = []      # (session resumed, prompt) of every work call
        inner = self.service.native_invoke

        def route_available():
            self.looks += 1
            return self.routes[min(self.looks, len(self.routes)) - 1] if self.routes else True
        self.service.route_available = route_available

        async def transport(agent, prompt, cwd, session_id=None, timeout=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            result = await inner(agent, prompt, cwd, session_id=session_id, timeout=timeout, **options)
            if context["job"]["job_phase"] != "work":
                return result
            self.work_calls.append((session_id, prompt))
            if len(self.cut_sessions) >= self.cuts:
                return result
            self.cut_sessions.append(result["session_id"])
            if self.last_words:
                options["on_activity"]({"type": "native_progress", "item_id": f"cut-{len(self.cut_sessions)}",
                                        "session_id": result["session_id"], "text": self.last_words})
            return {**result, "status": "failed", "failure_code": "cli_nonzero_exit", "text": "",
                    "native_execution": {**result["native_execution"], "tool_calls": 3, "tool_results": 3,
                                         "side_effects_possible": True, "exit_code": 1}}
        self.service.native_invoke = transport

    async def kinds(self):
        return [(event["kind"], event.get("code")) for event in (await self.state())["events"]]

    async def test_a_call_cut_off_while_offline_goes_on_in_the_same_session_when_the_network_is_back(self):
        # Catches the real stop: a partner three hours into a task lost the network for twenty minutes, the
        # whole room stopped, and the task was restarted twice in new sessions that had to find their place again.
        self.plan = [plan("T1")]
        self.routes = [False, False, True]   # offline when the call ends, still offline at the first look
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(len(self.work_calls), 2)
        first, second = self.work_calls
        self.assertEqual(second[0], self.cut_sessions[0], "the call after the outage resumed the session that was cut off")
        self.assertTrue(second[1].startswith(room_service.CONNECTION_RESTORED_NOTICE))
        self.assertEqual(second[1][len(room_service.CONNECTION_RESTORED_NOTICE):], first[1], "the task itself was given again unchanged")
        kinds = await self.kinds()
        self.assertEqual([code for kind, code in kinds if kind == "partner_call_retry"], ["connection_lost", "connection_restored"])
        self.assertNotIn("objective_stopped", [kind for kind, _ in kinds])
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], [])
        self.assertEqual(self.looks, 3)
        work = next(job for job in state["jobs"] if job["kind"] == "team_work")
        member = next(member for member in state["members"] if member["id"] == work["provider"])
        self.assertEqual(member["session_id"], self.cut_sessions[0])
        with self.service.store._connection() as db:
            observation = json.loads(db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (work["id"],)).fetchone()[0])
        # What the cut-off part did is counted with the rest: 3 tool calls before the outage, 1 after.
        self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["side_effects_possible"]), (4, 4, True))

    async def test_the_tools_own_network_error_is_enough_when_the_network_is_already_back(self):
        # Catches the fix only working while the machine is still offline at the moment the tool gives up:
        # tools retry for minutes on their own, and the network is often back by then.
        self.plan = [plan("T1")]
        self.last_words = ERROR_WORDS
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual([session for session, _ in self.work_calls], [self.cut_sessions[0]] * 2, "one call, taken up again in its session")
        self.assertNotIn("objective_stopped", [kind for kind, _ in await self.kinds()])

    async def test_a_call_that_fails_for_another_reason_is_not_resumed(self):
        # Catches every failed call being retried in place: with the network there and no network error
        # in the tool's words, the failure is the room's to handle as before.
        self.plan = [plan("T1")]
        self.last_words = "I could not finish: the build tool crashed."
        self.cuts = 99
        await self.create(["claude", "codex"])
        for _ in range(500):
            if any(kind == "objective_stopped" for kind, _ in await self.kinds()):
                break
            await asyncio.sleep(0.01)
        kinds = await self.kinds()
        self.assertIn(("objective_stopped", "cli_nonzero_exit"), kinds)
        self.assertEqual([code for kind, code in kinds if kind == "partner_call_retry"], [])
        self.assertEqual(len(self.work_calls), 1, "the failed call was not made again in place")
        await self.service.cancel_room(self.room_id)

    async def test_an_outage_that_keeps_ending_the_call_is_given_up_after_six_tries(self):
        # Catches a call resumed forever when the tool keeps failing with a network error.
        self.plan = [plan("T1")]
        self.last_words = ERROR_WORDS
        self.cuts = 99
        await self.create(["claude", "codex"])
        for _ in range(1000):
            if any(kind == "objective_stopped" for kind, _ in await self.kinds()):
                break
            await asyncio.sleep(0.01)
        kinds = await self.kinds()
        self.assertEqual([code for kind, code in kinds if kind == "partner_call_retry"].count("connection_lost"), 6)
        self.assertIn(("objective_stopped", "cli_nonzero_exit"), kinds)
        self.assertEqual([session for session, _ in self.work_calls[:7]], [self.cut_sessions[0]] * 7, "one call and six resumes of its session")
        self.assertEqual(len(self.work_calls), 7, "after the sixth resume the failure is the room's to handle")
        await self.service.cancel_room(self.room_id)

    async def test_stopping_the_room_while_it_waits_for_the_network_ends_the_wait(self):
        # Catches a stop that hangs (or a room left locked) because a call is waiting for a network that never returns.
        self.plan = [plan("T1")]
        self.routes = [False]
        await self.create(["claude", "codex"])
        for _ in range(500):
            if ("partner_call_retry", "connection_lost") in await self.kinds():
                break
            await asyncio.sleep(0.01)
        self.assertIn(("partner_call_retry", "connection_lost"), await self.kinds())
        stopped = await asyncio.wait_for(self.service.cancel_room(self.room_id), 10)
        self.assertEqual([job["status"] for job in stopped["jobs"] if job["status"] in {"working", "queued"}], [])
        self.assertEqual(len(self.work_calls), 1, "nothing was called again after the stop")


class NetworkState(pool.unittest.TestCase):
    def test_only_the_systems_network_errors_count(self):
        # Catches ordinary failure text (or a model merely talking about networks) being read as an outage.
        self.assertEqual([network_state.names_network_error(text) for text in (
            ERROR_WORDS, "connect ETIMEDOUT 203.0.113.7:443", "error: Network is unreachable", "getaddrinfo failed",
            "All 12 tests pass.", "The network layer of the game needs a rewrite.", "", None, 42)],
            [True, True, True, True, False, False, False, False, False])

    def test_a_route_lookup_sends_nothing_and_reports_no_network(self):
        # Catches the probe opening a real connection, and "no route" being reported as a network.
        opened = []

        class Socket:
            def __init__(self, family, kind):
                opened.append((family, kind))
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def connect(self, address):
                raise OSError(51, "Network is unreachable")
        with patch.object(network_state.socket, "socket", Socket):
            self.assertFalse(network_state.route_available())
        self.assertEqual([kind for _, kind in opened], [network_state.socket.SOCK_DGRAM] * 2)
