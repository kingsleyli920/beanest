"""The room page and the room list read one stop_kind, judged like the host's own resumes."""
import unittest

from local_agent_coordinator.room_service import RoomService


def snapshot(*, status="stopped", phase="stopped", reason="service_shutdown", mode="autonomous", failover=True,
             lifecycle="active", origin=None, tasks=()):
    return {"room": {"id": "room", "lifecycle_state": lifecycle},
            "objective": {"status": status, "execution_policy": {"mode": mode, "quota_failover": failover},
                          "next_step": {"origin": origin} if origin else None},
            "team": {"phase": phase, "stop_reason": reason, "tasks": list(tasks)}}


class Store:
    def __init__(self, repeated=False):
        self.repeated = repeated

    def auto_continue_state(self, room_id):
        return "fingerprint", self.repeated


def service(repeated=False):
    value = RoomService.__new__(RoomService)
    value.store = Store(repeated)
    value._stop_kinds = {}
    return value


RESTART = {"action": "restart-objective", "can_send": True}


class StopKindTest(unittest.TestCase):
    def test_a_stop_the_host_continues_reads_auto_only_while_the_continue_can_be_sent(self):
        self.assertEqual(service()._stop_kind(snapshot(), RESTART), "auto")
        self.assertIsNone(service()._stop_kind(snapshot(), {**RESTART, "can_send": False}))
        self.assertIsNone(service()._stop_kind(snapshot(), {**RESTART, "action": "message"}))
        self.assertIsNone(service()._stop_kind(snapshot(), None))

    def test_a_stop_that_came_back_after_one_automatic_continue_is_the_users(self):
        self.assertIsNone(service(repeated=True)._stop_kind(snapshot(), RESTART))

    def test_user_and_keep_scene_stops_wait_for_the_user(self):
        for reason in ("cancel_requested", "objective_calls_exhausted", "native_cleanup_unconfirmed", "execution_unknown"):
            self.assertIsNone(service()._stop_kind(snapshot(reason=reason), RESTART), reason)

    def test_all_partners_out_of_quota_is_a_wait_only_with_quota_failover(self):
        self.assertEqual(service()._stop_kind(snapshot(reason="all_partners_quota_exhausted"), None), "quota_wait")
        self.assertIsNone(service()._stop_kind(snapshot(reason="all_partners_quota_exhausted", failover=False), None))

    def test_only_an_active_autonomous_objective_resumes_by_itself(self):
        self.assertIsNone(service()._stop_kind(snapshot(mode="step"), RESTART))
        self.assertIsNone(service()._stop_kind(snapshot(lifecycle="archived"), RESTART))

    def test_a_task_block_resumes_by_itself_only_when_every_block_is_recoverable(self):
        host = {"status": "blocked", "block_cause": None, "blocked_reason": "team_task_permission"}
        replan = {"status": "blocked", "block_cause": "coordination_required", "blocked_reason": "需要补一个任务"}
        question = {"status": "blocked", "block_cause": "user_decision", "blocked_reason": "用哪个方案？"}
        block = dict(status="needs_user", phase="working", reason=None, origin="task_block")
        self.assertEqual(service()._stop_kind(snapshot(**block, tasks=[host, replan]), None), "auto")
        self.assertIsNone(service()._stop_kind(snapshot(**block, tasks=[host, question]), None))
        self.assertIsNone(service(repeated=True)._stop_kind(snapshot(**block, tasks=[host]), None))

    def test_the_list_shows_an_automatic_continue_only_after_the_room_read_judged_the_same_stop(self):
        # The list poll never runs the full read, so it cannot see whether the continue can be sent.
        value, listed = service(), snapshot()
        listed["current_run"] = {"id": "run"}
        self.assertIsNone(value._listed_stop_kind(listed))
        value._stop_kinds["room"] = (value._stop_kind_key(listed), "auto")
        self.assertEqual(value._listed_stop_kind(listed), "auto")
        value._stop_kinds["room"] = (value._stop_kind_key(listed), None)
        self.assertIsNone(value._listed_stop_kind(listed), "the room read found the continue unsendable")
        moved = snapshot(reason="cli_nonzero_exit"); moved["current_run"] = {"id": "run"}
        value._stop_kinds["room"] = (value._stop_kind_key(listed), "auto")
        self.assertIsNone(value._listed_stop_kind(moved), "a judgement of another stop is not reused")
        self.assertEqual(value._listed_stop_kind(snapshot(reason="all_partners_quota_exhausted")), "quota_wait")


if __name__ == "__main__":
    unittest.main()
