"""Loopback HTTP/SQLite task flow through an owned fictional Kimi ACP process.

No installed member CLI or model is invoked. The service's production response
parser receives the original fenced text returned over real JSON-RPC stdio.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator import kimi_acp, native_adapters
from test_kimi_acp import FAKE_ACP, SID
from test_kimi_native_setup import configuration
from test_kimi_setup_http import SetupFixture
from test_native_room_service import NativeTransport, native_reply
from test_team_store import proposal


class FencedACPFixture(SetupFixture):
    """Reusable disposable HTTP server; create() starts only fictional work."""

    def __init__(self, *, case="completed", malformed=False):
        super().__init__(discover=False)
        self.config.write_text(configuration(), encoding="utf-8")
        self.executable.write_text(f"#!{sys.executable}\n" + FAKE_ACP, encoding="utf-8")
        self.stack.enter_context(patch.object(kimi_acp.Path, "home", return_value=self.home))
        self.case, self.malformed = case, malformed
        self.native_calls = []
        self.peer = NativeTransport()
        self.peer.handler = self.reply
        self.service.native_invoke = self.invoke
        code, value = self.request("POST", "/api/partners/discover", {})
        if code != 200:
            raise AssertionError(value)
        code, value = self.request("POST", "/api/projects", {
            "name": "Fictional fenced ACP project", "path": str(self.source), "selection_kind": "folder"})
        if code != 200:
            raise AssertionError(value)
        self.project_id = value["project"]["id"]

    @staticmethod
    def reply(agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        commands = []
        if phase in {"plan", "coordinate"}:
            if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                commands = [proposal("T1", paths=[]), {"type": "claim", "task_id": "T1"}]
            commands.append({"type": "plan_ready"})
        elif phase == "work":
            commands = [{"type": "complete", "task_id": task["id"], "result": "Fictional text deliverable checked."}]
        elif phase == "review":
            commands = [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked exact text result."}]
        elif phase == "checkpoint":
            commands = [{"type": "checkpoint", "decision": "complete", "summary": "Fictional request completed.",
                         "next_goal": None, "question": None, "scope_reference": context["objective"]["scope_reference"]}]
        elif phase == "checkpoint_review":
            commands = [{"type": "checkpoint_review", "verdict": "accept", "reason": "Matches fictional request.",
                         "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}]
        elif phase != "user":
            raise AssertionError(phase)
        return native_reply(context, commands)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        record = {"agent": agent, "phase": context["job"]["job_phase"], "permission": options.get("permission")}
        self.native_calls.append(record)
        if agent != "kimi":
            return await self.peer(agent, prompt, cwd, session_id=session_id, **options)
        final = "{broken JSON" if self.malformed else json.dumps(self.reply(agent, context, cwd))
        (self.root / "scenario.json").write_text(json.dumps({
            "session_id": session_id or SID, "case": self.case,
            "final": "```json\n" + final + "\n```", "no_tools": True,
        }), encoding="utf-8")
        result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
        record.update(status=result["status"], error=result.get("failure_code"),
                      cleanup_confirmed=result.get("cleanup_confirmed"),
                      fenced_text_preserved=result.get("text", "").startswith("```json\n"))
        return result

    def create(self):
        code, value = self.request("POST", "/api/rooms", {
            "goal": "Prepare and independently check a short fictional text deliverable.",
            "target": "kimi", "request_id": str(uuid4()), "mode": "native_project",
            "project_id": self.project_id, "participants": ["kimi", "claude"], "work_mode": "build",
            "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        if code != 200:
            raise AssertionError(value)
        self.room_id = value["room"]["id"]
        return value

    def settled(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            code, value = self.request("GET", f"/api/rooms/{self.room_id}")
            if code != 200:
                raise AssertionError(value)
            if (not any(job["status"] in {"queued", "working"} for job in value["jobs"])
                    and not any(not worker.done() for worker in list(self.service._workers.values()))):
                return value
            time.sleep(.01)
        raise AssertionError("Fictional ACP flow did not settle")


class KimiFencedHTTPTests(unittest.TestCase):
    def fixture(self, **kwargs):
        value = FencedACPFixture(**kwargs)
        self.addCleanup(value.close)
        return value

    def test_complete_fenced_team_response_passes_real_acp_and_http_taskflow(self):
        fixture = self.fixture()
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertTrue(all(job["error"] is None for job in final["jobs"]))
        calls = fixture.native_calls
        self.assertTrue(any(call["agent"] == "claude" and call["phase"] == "review" for call in calls))
        kimi = [call for call in calls if call["agent"] == "kimi"]
        self.assertEqual({call["phase"] for call in kimi}, {"plan", "work", "checkpoint"})
        self.assertTrue(all(call["status"] == "completed" and call["cleanup_confirmed"]
                            and call["fenced_text_preserved"] for call in kimi))
        self.assertTrue(all(call["permission"] == "full_access" for call in calls))
        trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
        self.assertEqual([row["modeId"] for row in trace if row.get("method") == "session/set_mode"], ["auto"] * len(kimi))

    def test_invalid_json_and_missing_native_terminal_are_not_accepted(self):
        for kwargs, expected in (({"malformed": True}, "invalid_agent_response"),
                                 ({"case": "missing_wire"}, "native_turn_unconfirmed")):
            with self.subTest(**kwargs):
                fixture = self.fixture(**kwargs)
                try:
                    fixture.create()
                    final = fixture.settled()
                    self.assertNotEqual(final["objective"]["status"], "complete")
                    self.assertTrue(any(job["error"] == expected for job in final["jobs"]),
                                    [(job["provider"], job["status"], job["error"]) for job in final["jobs"]])
                    self.assertFalse(any(task["status"] == "accepted" for task in final["team"]["tasks"]))
                    kimi = next(call for call in fixture.native_calls if call["agent"] == "kimi")
                    # The native process can complete even when the host rejects
                    # its business JSON. Missing wire remains a native failure.
                    self.assertEqual(kimi["error"], None if kwargs.get("malformed") else expected)
                    self.assertEqual(kimi["status"], "completed" if kwargs.get("malformed") else "failed")
                    self.assertTrue(kimi["cleanup_confirmed"])
                finally:
                    fixture.close()


if __name__ == "__main__":
    unittest.main()
