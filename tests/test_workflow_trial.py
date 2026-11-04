"""The experiment driver uses fake peers, a real private journal and one fake loop."""
import asyncio
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from local_agent_coordinator.workflow_trial import (
    JOURNAL_NAME, RESULTS_NAME, TrialError, TrialJournal, WorkflowTrial,
    create_trial_server, load_plan, main, validate_plan, validate_limits,
)


DIGEST = "a" * 64


def plan(count=2):
    return {"goal": "Private overall goal", "phases": [
        {"title": f"Phase {number + 1}", "goal": f"Private phase goal {number + 1}",
         "target": "claude" if number % 2 == 0 else "codex"} for number in range(count)]}


def checkpoint(room_id, *, used=3, executions=1, status="completed", settled=True):
    runs = [{"revision": 0, "snapshot_sha256": "b" * 64, "status": "failed",
             "result": {"status": "failed", "exit_code": 1, "cleanup_confirmed": True}}
            for _ in range(executions - 1)]
    runs.append({"revision": 1, "snapshot_sha256": DIGEST, "status": "completed",
                 "result": {"status": "completed", "exit_code": 0,
                            "cleanup_confirmed": True, "stdout": "not trusted acceptance"}})
    return {"settled": settled, "project_digest": DIGEST, "snapshot": {
        "room": {"id": room_id, "status": status, "mode": "python_sandbox"},
        "budget": {"used": used, "limit": 8}, "executions": runs,
        "members": [{"id": identity, "adapter": identity} for identity in ("claude", "codex")],
        "project": {"available": True, "revision": 1,
                    "files": [{"path": "score.py", "content": "PRIVATE ARTIFACT"}]},
        "jobs": [{"provider": identity, "status": "completed"} for identity in ("claude", "codex")],
        "events": [{"text": "PRIVATE MODEL MESSAGE"}],
    }}


class FakeLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.shutdown_error = None
        self.calls = 0

    def call(self, operation):
        self.calls += 1
        return self.loop.run_until_complete(operation())

    def close(self):
        self.loop.close()


class FakeService:
    def __init__(self, runtime, responses=None):
        self.runtime = runtime
        self.responses = responses or []
        self.creations = []
        self.checks = []
        self.loop = None
        self.start_error = False
        self.check_error = False
        self.on_check = None
        self.shutdown_cleanup_confirmed = None
        self.cleanup_result = True

    def assert_loop(self):
        assert asyncio.get_running_loop() is self.loop

    async def create_artifact_phase(self, goal, target, request_id, participants, **source):
        self.assert_loop()
        recorded = json.loads((self.runtime / JOURNAL_NAME).read_text())
        pending = recorded["phases"][len(self.creations)]
        assert pending["request_id"] == request_id and pending["status"] == "prepared"
        self.creations.append({"goal": goal, "target": target, "request_id": request_id,
                               "participants": participants, "source": source})
        if self.start_error:
            raise RuntimeError("private provider detail")
        room_id = f"room-{len(self.creations)}"
        return {"room": {"id": room_id}}

    async def phase_checkpoint(self, room_id):
        self.assert_loop()
        self.checks.append(room_id)
        if self.check_error:
            raise RuntimeError("private provider detail")
        if self.on_check is not None:
            return self.on_check(room_id)
        index = int(room_id.split("-")[1]) - 1
        result = self.responses[index] if index < len(self.responses) else checkpoint(room_id)
        return copy.deepcopy(result)

    async def shutdown(self):
        self.shutdown_cleanup_confirmed = self.cleanup_result


class FakeServer:
    def __init__(self, service):
        self.service = service
        self.runner = FakeLoop()
        service.loop = self.runner.loop
        self.url = "http://127.0.0.1:12345/"
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self):
        self.stopped.wait(2)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True
        self.service.shutdown_cleanup_confirmed = self.service.cleanup_result
        self.runner.close()


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "trial"
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            if not server.closed:
                server.server_close()
        self.temporary.cleanup()

    def driver(self, journal, phases=None, responses=None, **limits):
        service = FakeService(self.runtime, responses)
        server = FakeServer(service)
        self.servers.append(server)
        return WorkflowTrial(server, journal, phases or plan(), **limits), service

    def test_plan_rejects_extra_keys_duplicates_invalid_targets_and_unbounded_inputs(self):
        for candidate in (plan(0), plan(17), {**plan(), "command": "shell"},
                          {"goal": "x", "phases": [{"title": "x", "goal": "x", "target": "all"}]},
                          {"goal": "x" * 3001, "phases": plan()["phases"]}):
            with self.subTest(candidate=str(candidate)[:50]), self.assertRaises(TrialError):
                validate_plan(candidate)
        source = self.root / "plan.json"
        source.write_text('{"goal":"one","goal":"two","phases":[]}')
        with self.assertRaises(TrialError):
            load_plan(source)

    def test_trial_limits_reject_boolean_nonfinite_fractional_counts_and_out_of_bounds(self):
        valid = {"max_calls": 96, "max_executions": 24, "total_timeout": 7200, "call_timeout": 600}
        invalid = {
            "max_calls": (True, False, 0, -1, 97, 95.5, 95.0, float('inf'), float('nan')),
            "max_executions": (True, False, 0, -1, 25, 2.5, 2.0, float('inf'), float('nan')),
            "total_timeout": (True, False, 0, -1, 7201, float('inf'), float('-inf'), float('nan')),
            "call_timeout": (True, False, 0, -1, 601, float('inf'), float('-inf'), float('nan')),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(TrialError) as error:
                    validate_limits(**{**valid, key: value})
                self.assertEqual(error.exception.code, "invalid_trial_limits")

    def test_subphase_remaining_budget_stops_before_any_dispatch(self):
        for index, remaining in enumerate(({"max_calls": 7}, {"max_executions": 1})):
            self.runtime = self.root / f"insufficient-{index}"
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, **remaining)
                result = driver.run()
            self.assertEqual(result["reason"], "trial_budget_exhausted")
            self.assertEqual(service.creations, [])
            self.assertEqual(service.checks, [])

    def test_library_source_limit_and_initial_origin_are_strictly_bounded(self):
        for limit in (True, False, 0, -1, 65537, 53248.0, float('nan')):
            with self.subTest(limit=limit), self.assertRaises(TrialError) as error:
                WorkflowTrial(None, None, plan(), source_byte_limit=limit)
            self.assertEqual(error.exception.code, 'invalid_trial_limits')
        for origin in ({}, {'room_id': 'old', 'revision': True, 'digest': DIGEST},
                       {'room_id': 'old', 'revision': 0, 'digest': DIGEST},
                       {'room_id': 'old', 'revision': 1, 'digest': 'invalid'},
                       {'room_id': 'old', 'revision': 1, 'digest': DIGEST, 'path': 'not allowed'}):
            with self.subTest(origin=origin), self.assertRaises(TrialError) as error:
                WorkflowTrial(None, None, plan(), initial_source=origin)
            self.assertEqual(error.exception.code, 'invalid_initial_source')

    def test_phase_source_limit_counts_actual_utf8_across_all_files(self):
        for index, (contents, accepted) in enumerate(((['豆' * 3, 'ab'], True), (['豆' * 2, '豆' * 2], False))):
            self.runtime = self.root / f'utf8-source-{index}'
            value = checkpoint('room-1')
            value['snapshot']['project']['files'] = [{'content': content} for content in contents]
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, plan(1), responses=[value], source_byte_limit=11)
                result = driver.run()
            self.assertEqual(result['caps']['source_byte_limit'], 11)
            self.assertEqual(result['status'], 'completed' if accepted else 'stopped')
            self.assertEqual(result['reason'], None if accepted else 'phase_source_limit_exceeded')
            self.assertEqual(len(service.creations), 1)

    def test_underlying_valid_artifact_above_plan_limit_never_advances_to_next_phase(self):
        value = checkpoint('room-1')
        value['snapshot']['project']['files'] = [{'content': 'x' * 58403}]
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, responses=[value], source_byte_limit=53248)
            result = driver.run()
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['reason'], 'phase_source_limit_exceeded')
        self.assertEqual(len(service.creations), 1)
        self.assertEqual(result['phases'][1]['status'], 'pending')
        self.assertIs(result['totals_complete'], False)

    def test_explicit_old_baseline_can_exceed_new_limit_but_children_and_following_origins_cannot(self):
        origin = {'room_id': 'old-room', 'revision': 1, 'digest': DIGEST}
        baseline = checkpoint('old-room', used=8, executions=2)
        baseline['snapshot']['project']['files'] = [{'content': 'x' * 60000}]
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, source_byte_limit=53248, initial_source=origin)
            origin['revision'] = 99  # Caller mutations cannot retarget the saved source.
            service.on_check = lambda room_id: copy.deepcopy(baseline) if room_id == 'old-room' else checkpoint(room_id)
            result = driver.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(service.checks[0], 'old-room')
        self.assertEqual(service.checks.count('old-room'), 1)
        self.assertEqual(result['initial_source'], {'room_id': 'old-room', 'revision': 1, 'digest': DIGEST})
        self.assertEqual(service.creations[0]['source'], {
            'source_room_id': 'old-room', 'source_revision': 1, 'source_digest': DIGEST})
        self.assertEqual(service.creations[1]['source'], {
            'source_room_id': 'room-1', 'source_revision': 1, 'source_digest': DIGEST})
        self.assertEqual(result['totals'], {'model_calls': 6, 'executions': 2})

    def test_initial_source_must_match_exact_room_revision_and_digest_before_create(self):
        expected = {'room_id': 'old-room', 'revision': 1, 'digest': DIGEST}
        for index, mutate in enumerate((lambda value: value['snapshot']['room'].update(id='another-room'),
                                        lambda value: value['snapshot']['project'].update(revision=2),
                                        lambda value: value.update(project_digest='c' * 64))):
            self.runtime = self.root / f'origin-mismatch-{index}'
            baseline = checkpoint('old-room')
            mutate(baseline)
            # Keep the execution valid for the changed origin, isolating exact-match validation.
            baseline['snapshot']['executions'][0].update(
                revision=baseline['snapshot']['project']['revision'], snapshot_sha256=baseline['project_digest'])
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, initial_source=expected)
                service.on_check = lambda _room, value=baseline: copy.deepcopy(value)
                result = driver.run()
            self.assertEqual(result['reason'], 'phase_source_unconfirmed')
            self.assertEqual(service.creations, [])

    def test_initial_source_still_requires_full_machine_gate_and_original_hard_limit(self):
        origin = {'room_id': 'old-room', 'revision': 1, 'digest': DIGEST}
        def oversized(value):
            value['snapshot']['project']['files'] = [{'content': 'x' * 65537}]
        def no_cleanup(value):
            value['snapshot']['executions'][0]['result']['cleanup_confirmed'] = False
        def one_peer(value):
            value['snapshot']['jobs'].pop()
        def active(value):
            value['settled'] = False
        for index, mutate in enumerate((oversized, no_cleanup, one_peer, active)):
            self.runtime = self.root / f'origin-gate-{index}'
            baseline = checkpoint('old-room')
            mutate(baseline)
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, initial_source=origin, source_byte_limit=53248)
                service.on_check = lambda _room, value=baseline: copy.deepcopy(value)
                result = driver.run()
            self.assertEqual(result['reason'], 'phase_source_limit_exceeded' if mutate is oversized else 'phase_gate_failed')
            self.assertEqual(service.creations, [])

    def test_new_child_of_explicit_baseline_must_satisfy_the_lower_source_limit(self):
        origin = {'room_id': 'old-room', 'revision': 1, 'digest': DIGEST}
        baseline = checkpoint('old-room')
        baseline['snapshot']['project']['files'] = [{'content': 'x' * 60000}]
        child = checkpoint('room-1')
        child['snapshot']['project']['files'] = [{'content': 'x' * 58403}]
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, source_byte_limit=53248, initial_source=origin)
            service.on_check = lambda room_id: copy.deepcopy(baseline if room_id == 'old-room' else child)
            result = driver.run()
        self.assertEqual(result['reason'], 'phase_source_limit_exceeded')
        self.assertEqual(len(service.creations), 1)
        self.assertEqual(result['phases'][1]['status'], 'pending')

    def test_journal_is_private_atomic_and_locked(self):
        with TrialJournal(self.runtime) as journal:
            driver, _service = self.driver(journal, plan(1))
            self.assertEqual(driver.run()["status"], "completed")
            with self.assertRaises(TrialError) as busy:
                TrialJournal(self.runtime)
            self.assertEqual(busy.exception.code, "trial_runtime_in_use")
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        for filename in (JOURNAL_NAME, RESULTS_NAME, ".workflow-trial.lock"):
            self.assertEqual((self.runtime / filename).stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(path.name.startswith(".workflow-trial-") for path in self.runtime.iterdir()))
        result = json.loads((self.runtime / RESULTS_NAME).read_text())
        self.assertEqual(result["checkpoint"]["snapshot"]["project"]["files"][0]["content"], "PRIVATE ARTIFACT")
        self.assertEqual(result["acceptance"], "not_independently_verified")

    def test_symlink_ancestor_journal_and_lock_are_rejected_without_following(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(TrialError):
            TrialJournal(alias / "child")
        self.assertFalse((real / "child").exists())
        target = self.root / "outside.json"
        target.write_text("unchanged")
        for filename in (JOURNAL_NAME, ".workflow-trial.lock"):
            runtime = self.root / filename.replace(".", "-")
            runtime.mkdir(mode=0o700)
            (runtime / filename).symlink_to(target)
            with self.subTest(filename=filename), self.assertRaises((TrialError, OSError)):
                TrialJournal(runtime)
        self.assertEqual(target.read_text(), "unchanged")

    def test_corrupt_journal_fails_closed_without_dispatch_or_reset(self):
        self.runtime.mkdir(mode=0o700)
        path = self.runtime / JOURNAL_NAME
        path.write_text('{"schema":1,"schema":2}')
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(TrialError):
            TrialJournal(self.runtime)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_prior_journal_and_removes_temporary_file(self):
        with TrialJournal(self.runtime) as journal:
            driver, _service = self.driver(journal, plan(1))
            driver.run()
            before = (self.runtime / JOURNAL_NAME).read_bytes()
            journal.data["reason"] = "uncommitted_change"
            with patch('local_agent_coordinator.workflow_trial.os.replace', side_effect=OSError("private path")), \
                 self.assertRaises(TrialError):
                journal.save()
            self.assertEqual((self.runtime / JOURNAL_NAME).read_bytes(), before)
            self.assertFalse(any(path.name.startswith(".workflow-trial-") for path in self.runtime.iterdir()))

    def test_two_phases_use_one_loop_and_pass_exact_source_revision_and_digest(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal)
            result = driver.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["target"] for item in service.creations], ["claude", "codex"])
        self.assertEqual([item["participants"] for item in service.creations], [["claude", "codex"]] * 2)
        self.assertEqual(service.creations[0]["source"], {})
        self.assertEqual(service.creations[1]["source"], {
            "source_room_id": "room-1", "source_revision": 1, "source_digest": DIGEST})
        self.assertNotEqual(service.creations[0]["request_id"], service.creations[1]["request_id"])
        self.assertEqual(result["totals"], {"model_calls": 6, "executions": 2})
        self.assertEqual(service.checks.count("room-1"), 2)

    def test_remaining_full_phase_reservation_uses_actual_calls_and_execution_counts(self):
        scenarios = [(9, 24, 2, 1, 1), (9, 24, 1, 1, 2), (96, 3, 3, 2, 1), (96, 3, 3, 1, 2)]
        for index, (calls, runs, used, executions, expected_creates) in enumerate(scenarios):
            self.runtime = self.root / f"budget-{index}"
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, responses=[checkpoint("room-1", used=used, executions=executions)],
                                              max_calls=calls, max_executions=runs)
                result = driver.run()
            self.assertEqual(len(service.creations), expected_creates)
            self.assertEqual(result["status"], "stopped" if expected_creates == 1 else "completed")
            if expected_creates == 1:
                self.assertEqual(result["reason"], "trial_budget_exhausted")

    def test_full_hard_caps_stop_before_thirteenth_maximum_phase(self):
        responses = [checkpoint(f"room-{index + 1}", used=8, executions=2) for index in range(16)]
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, plan(16), responses)
            result = driver.run()
        self.assertEqual(len(service.creations), 12)
        self.assertEqual(result["totals"], {"model_calls": 96, "executions": 24})
        self.assertEqual(result["reason"], "trial_budget_exhausted")
        self.assertEqual(result["phases"][12]["status"], "pending")

    def test_prior_room_counts_are_refreshed_before_next_phase(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, max_calls=9)
            def changing(room_id):
                return checkpoint(room_id, used=1 if len(service.checks) == 1 else 8)
            service.on_check = changing
            result = driver.run()
        self.assertEqual(len(service.creations), 1)
        self.assertEqual(result["totals"]["model_calls"], 8)
        self.assertEqual(result["reason"], "trial_budget_exhausted")

    def test_regressing_observed_counters_stops_with_incomplete_totals(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal)
            service.on_check = lambda room_id: checkpoint(room_id, used=3 if len(service.checks) == 1 else 2)
            result = driver.run()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "phase_accounting_unknown")
        self.assertIs(result["totals_complete"], False)
        self.assertEqual(len(service.creations), 1)

    def test_failure_and_unknown_never_start_the_next_phase(self):
        for status in ("failed", "unknown", "needs_user", "budget_exhausted"):
            self.runtime = self.root / status
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, responses=[checkpoint("room-1", status=status, settled=False)])
                result = driver.run()
            self.assertEqual(len(service.creations), 1)
            self.assertEqual(result["reason"], "phase_" + status)

    def test_gate_requires_two_actual_peers_current_execution_and_confirmed_cleanup(self):
        def one_peer(value):
            value["snapshot"]["jobs"].pop()
        def no_artifacts(value):
            value["snapshot"]["project"]["available"] = False
        def old_execution(value):
            value["snapshot"]["executions"][0]["revision"] = 0
        def wrong_digest(value):
            value["snapshot"]["executions"][0]["snapshot_sha256"] = "c" * 64
        def no_cleanup(value):
            value["snapshot"]["executions"][0]["result"]["cleanup_confirmed"] = False
        def lying_output(value):
            result = value["snapshot"]["executions"][0]["result"]
            result.update(exit_code=1, stdout="ALL TESTS PASSED")
        for number, mutate in enumerate((one_peer, no_artifacts, old_execution, wrong_digest, no_cleanup, lying_output)):
            self.runtime = self.root / f"gate-{number}"
            value = checkpoint("room-1")
            mutate(value)
            with TrialJournal(self.runtime) as journal:
                driver, service = self.driver(journal, responses=[value])
                result = driver.run()
            self.assertEqual(result["reason"], "phase_gate_failed")
            self.assertEqual(len(service.creations), 1)

    def test_deadline_stops_unsettled_work_without_opening_another_room(self):
        now = [0.0]
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, responses=[checkpoint("room-1", settled=False)],
                                          monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
                                          total_timeout=2)
            result = driver.run()
        self.assertEqual(now[0], 2)
        self.assertEqual(result["reason"], "trial_deadline_exceeded")
        self.assertEqual(len(service.creations), 1)

    def test_unknown_create_has_durable_key_and_reopen_does_not_retry_even_changed_plan(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal)
            service.start_error = True
            result = driver.run()
            key = result["phases"][0]["request_id"]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "phase_start_unknown")
        self.assertIs(result["totals_complete"], False)
        with TrialJournal(self.runtime) as journal:
            other, service = self.driver(journal, plan(1), initial_source={
                'room_id': 'old-room', 'revision': 1, 'digest': DIGEST})
            replay = other.run()
            self.assertEqual(service.creations, [])
            self.assertEqual(service.checks, [])
            self.assertIsNone(replay['initial_source'])
            self.assertEqual(replay["phases"][0]["request_id"], key)
            self.assertEqual(len(replay["phases"]), 2)

    def test_unknown_later_start_keeps_last_observation_attributed_to_its_original_phase(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal)
            def stop_next_start(room_id):
                service.start_error = True
                return checkpoint(room_id)
            service.on_check = stop_next_start
            result = driver.run()
        self.assertEqual(result["reason"], "phase_start_unknown")
        observed = json.loads((self.runtime / RESULTS_NAME).read_text())
        self.assertEqual(observed["phase_index"], 0)
        self.assertEqual(observed["active_phase_index"], 1)
        self.assertEqual(observed["room_id"], "room-1")
        self.assertIs(observed["terminal"], True)
        self.assertIs(observed["final_for_trial"], False)
        self.assertEqual(observed["observation_scope"], "last_observed_before_shutdown")

    def test_result_write_failure_retains_terminal_core_journal_without_replaying(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal, plan(1))
            with patch.object(journal, 'save_result', side_effect=TrialError('trial_journal_write_failed')):
                result = driver.run()
            recorded = json.loads((self.runtime / JOURNAL_NAME).read_text())
        self.assertEqual(recorded["status"], "unknown")
        self.assertEqual(recorded["reason"], "trial_result_unconfirmed")
        self.assertIs(result["totals_complete"], False)
        self.assertEqual(len(service.creations), 1)

    def test_crash_reopen_marks_interrupted_and_never_dispatches(self):
        with TrialJournal(self.runtime) as journal:
            driver, service = self.driver(journal)
            service.on_check = lambda _room: (_ for _ in ()).throw(KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                driver.run()
            # Model a hard crash before the interruption marker could be written.
            journal.data["status"] = "running"
            journal.save()
        with TrialJournal(self.runtime) as journal:
            replay, service = self.driver(journal)
            result = replay.run()
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(service.creations, [])
            self.assertEqual(service.checks, [])

    def test_status_is_read_only_and_does_not_construct_any_service_or_print_content(self):
        with TrialJournal(self.runtime) as journal:
            driver, _service = self.driver(journal, plan(1))
            driver.run()
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.runtime.iterdir()}
        output = io.StringIO()
        with patch('local_agent_coordinator.room_service.RoomService', side_effect=AssertionError), \
             redirect_stdout(output):
            self.assertEqual(main(["--runtime", str(self.runtime), "--status"]), 0)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.runtime.iterdir()}
        self.assertEqual(after, before)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("Private", output.getvalue())

    def test_cli_refuses_any_existing_service_state_before_constructing_service(self):
        for name in ("rooms.sqlite3", "workspaces", "project-artifacts", "partners", "anything-else"):
            self.runtime = self.root / name.replace(".", "-")
            self.runtime.mkdir(mode=0o700)
            target = self.runtime / name
            if name in {"workspaces", "project-artifacts", "partners"}:
                target.mkdir(mode=0o700)
            else:
                target.write_text("DO NOT RECOVER")
            before = target.stat().st_mtime_ns
            output = io.StringIO()
            with patch('local_agent_coordinator.room_service.RoomService', side_effect=AssertionError) as factory, \
                 redirect_stderr(output):
                self.assertEqual(main(["--runtime", str(self.runtime)]), 2)
            factory.assert_not_called()
            self.assertEqual(output.getvalue().strip(), "trial_runtime_not_empty")
            self.assertEqual(target.stat().st_mtime_ns, before)

    def test_http_trial_handler_rejects_posts_before_service_dispatch(self):
        class Unserved:
            def __init__(self, service, **_options):
                self.service = service
        with patch('local_agent_coordinator.room_server.RoomUIServer', Unserved):
            server = create_trial_server(types.SimpleNamespace(), 0)
        handler = types.SimpleNamespace(command="POST", server=server)
        from local_agent_coordinator.ui_server import _HTTPError
        with self.assertRaises(_HTTPError) as error:
            asyncio.run(server.RequestHandlerClass._dispatch_room(handler, "/api/rooms", "", {}))
        self.assertEqual(error.exception.status, 403)

    def test_cli_closes_http_and_service_and_persists_cleanup_with_metadata_output(self):
        source = self.root / "plan.json"
        source.write_text(json.dumps(plan(1)))
        runner_root = self.root / "runner"
        runner_root.mkdir(mode=0o700)
        service = FakeService(self.runtime)
        server = FakeServer(service)
        self.servers.append(server)
        output = io.StringIO()
        with patch('local_agent_coordinator.room_service.RoomService', return_value=service) as factory, \
             patch('local_agent_coordinator.python_runner.PythonRunner', return_value=object()), \
             patch('local_agent_coordinator.workflow_trial.create_trial_server', return_value=server), \
             redirect_stdout(output):
            result = main(["--runtime", str(self.runtime), "--plan", str(source),
                           "--runner-root", str(runner_root)])
        self.assertEqual(result, 0)
        self.assertTrue(server.closed)
        self.assertTrue(server.stopped.is_set())
        recorded = json.loads((self.runtime / JOURNAL_NAME).read_text())
        self.assertIs(recorded["cleanup_confirmed"], True)
        self.assertEqual(factory.call_args.kwargs['call_timeout'], 600)
        self.assertEqual(recorded["caps"]["call_timeout"], 600)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("Private", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_cli_custom_remaining_caps_and_trial_timeout_reach_service_and_journal(self):
        source = self.root / "plan.json"
        source.write_text(json.dumps(plan(1)))
        runner_root = self.root / "runner"
        runner_root.mkdir(mode=0o700)
        service = FakeService(self.runtime)
        server = FakeServer(service)
        self.servers.append(server)
        with patch('local_agent_coordinator.room_service.RoomService', return_value=service) as factory, \
             patch('local_agent_coordinator.python_runner.PythonRunner', return_value=object()), \
             patch('local_agent_coordinator.workflow_trial.create_trial_server', return_value=server), \
             redirect_stdout(io.StringIO()):
            result = main(["--runtime", str(self.runtime), "--plan", str(source),
                           "--runner-root", str(runner_root), "--seconds", "3000.5", "--call-timeout", "480",
                           "--max-calls", "95", "--max-executions", "23"])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs['call_timeout'], 480)
        caps = json.loads((self.runtime / JOURNAL_NAME).read_text())["caps"]
        self.assertEqual(caps, {"seconds": 3000.5, "model_calls": 95, "executions": 23,
                                "call_timeout": 480, "source_byte_limit": 65536,
                                "phase_model_calls": 8, "phase_executions": 2})

    def test_invalid_cli_limits_do_not_create_runtime_or_construct_service(self):
        values = (("--call-timeout", "nan"), ("--call-timeout", "601"), ("--seconds", "inf"),
                  ("--seconds", "7201"), ("--max-calls", "97"), ("--max-executions", "25"))
        with patch('local_agent_coordinator.room_service.RoomService', side_effect=AssertionError) as factory:
            for option, value in values:
                with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--runtime", str(self.runtime), option, value]), 2)
            for option, value in (("--max-calls", "95.5"), ("--call-timeout", "true")):
                with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()), \
                     self.assertRaises(SystemExit) as error:
                    main(["--runtime", str(self.runtime), option, value])
                self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()
        self.assertFalse(self.runtime.exists())

    def test_cli_cleanup_failure_is_recorded_and_does_not_claim_complete_totals(self):
        source = self.root / "plan.json"
        source.write_text(json.dumps(plan(1)))
        runner_root = self.root / "runner"
        runner_root.mkdir(mode=0o700)
        service = FakeService(self.runtime)
        server = FakeServer(service)
        self.servers.append(server)
        server.runner.shutdown_error = "shutdown_timeout"
        with patch('local_agent_coordinator.room_service.RoomService', return_value=service), \
             patch('local_agent_coordinator.python_runner.PythonRunner', return_value=object()), \
             patch('local_agent_coordinator.workflow_trial.create_trial_server', return_value=server), \
             redirect_stdout(io.StringIO()):
            result = main(["--runtime", str(self.runtime), "--plan", str(source),
                           "--runner-root", str(runner_root)])
        self.assertEqual(result, 2)
        recorded = json.loads((self.runtime / JOURNAL_NAME).read_text())
        self.assertEqual(recorded["status"], "unknown")
        self.assertEqual(recorded["reason"], "trial_cleanup_unconfirmed")
        self.assertIs(recorded["cleanup_confirmed"], False)
        self.assertIs(recorded["totals_complete"], False)

    def test_clean_loop_shutdown_requires_explicit_service_resource_confirmation(self):
        for index, confirmation in enumerate((False, None)):
            self.runtime = self.root / f"unconfirmed-{index}"
            source = self.root / f"plan-{index}.json"
            source.write_text(json.dumps(plan(1)))
            runner_root = self.root / f"runner-{index}"
            runner_root.mkdir(mode=0o700)
            service = FakeService(self.runtime)
            service.cleanup_result = confirmation
            server = FakeServer(service)
            self.servers.append(server)
            with patch('local_agent_coordinator.room_service.RoomService', return_value=service), \
                 patch('local_agent_coordinator.python_runner.PythonRunner', return_value=object()), \
                 patch('local_agent_coordinator.workflow_trial.create_trial_server', return_value=server), \
                 redirect_stdout(io.StringIO()):
                result = main(["--runtime", str(self.runtime), "--plan", str(source),
                               "--runner-root", str(runner_root)])
            self.assertIsNone(server.runner.shutdown_error)
            self.assertEqual(result, 2)
            recorded = json.loads((self.runtime / JOURNAL_NAME).read_text())
            self.assertEqual(recorded["status"], "unknown")
            self.assertEqual(recorded["reason"], "trial_cleanup_unconfirmed")
            self.assertIs(recorded["cleanup_confirmed"], False)
            self.assertIs(recorded["totals_complete"], False)


if __name__ == "__main__":
    unittest.main()
