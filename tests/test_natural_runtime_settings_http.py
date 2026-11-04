"""Actual loopback settings/receipt requests with synthetic native calls only."""
import asyncio
import threading
import unittest

import test_native_room_http as http_native
import test_native_room_service as native
from test_natural_runtime_settings import settings


class NaturalRuntimeSettingsHTTPTests(unittest.TestCase):
    setUp = http_native.NativeRoomHTTPTests.setUp
    tearDown = http_native.NativeRoomHTTPTests.tearDown
    request = http_native.NativeRoomHTTPTests.request
    configure = http_native.NativeRoomHTTPTests.configure
    wait = http_native.NativeRoomHTTPTests.wait
    handler = native.NativeRoomServiceTests.handler

    def test_optional_settings_are_atomic_across_http_and_exact_receipt_with_actual_next_call_values(self):
        _, project = self.configure()
        for agent in ("claude", "codex"):
            self.service._runtime_catalogs[agent] = {"state": "ready", "models": [
                {"id": model, "efforts": ["low", "high"]} for model in ("old", "new")]}
        started, release = threading.Event(), threading.Event()
        def old(agent, context, cwd):
            if context["job"]["job_phase"] == "user":
                return native.native_reply(context)
            return self.handler(agent, context, cwd)
        async def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                started.set()
                while not release.is_set():
                    await asyncio.sleep(0.005)
            return old(agent, context, cwd)
        self.native.handler = handler
        code, created = self.request("POST", "/api/rooms", {"goal": "Update the value and retain useful notes.",
            "target": "claude", "request_id": "settings-http-create", "mode": "native_project", "project_id": project,
            "participants": ["claude", "codex"], "work_mode": "build", "runtime_settings": settings(),
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        room = created["room"]["id"]
        self.assertTrue(started.wait(3))
        try:
            code, before = self.request("GET", "/api/rooms/" + room)
            route = f"/api/rooms/{room}/messages"
            body = {"text": "Discuss the result with these later settings.", "target": "all", "request_id": "settings-http-send",
                    "intent": "continue", "binding": before["continuation"]["binding"],
                    "work_mode": "plan", "runtime_settings": settings("new", "high")}
            self.assertEqual(self.request("GET", "/api/status")[1]["capabilities"]["natural_runtime_settings"], True)
            code, accepted = self.request("POST", route, body)
            self.assertEqual(code, 200, accepted)
            self.assertTrue(accepted["runtime_configuration"]["pending"])
            self.assertEqual(accepted["room"]["work_mode"], "build")
            self.assertEqual(accepted["runtime_configuration"]["desired"]["work_mode"], "plan")
            calls = len(self.native.calls)
            self.service._runtime_catalogs.clear()
            self.assertEqual(self.request("POST", route + "/receipt", body),
                             (200, {"received": True, "room_id": room, "request_id": body["request_id"]}))
            self.assertEqual(self.request("POST", route, body)[0], 200)
            self.assertEqual(len(self.native.calls), calls)
            self.assertEqual(self.request("POST", route + "/receipt", body | {"work_mode": "build"})[0], 409)
            self.assertEqual(self.request("POST", route, body | {"runtime_settings": None})[0], 400)
            self.assertEqual(self.request("POST", f"/api/mcp/rooms/{room}/messages", body, mcp=True)[0], 403)
        finally:
            release.set()
        final = self.wait(room, lambda snapshot: not snapshot["runtime_configuration"]["pending"]
                          and not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]))
        self.assertEqual(final["room"]["work_mode"], "plan")
        later = [call for call in self.native.calls if call["context"]["work_mode"] == "plan"]
        self.assertTrue(later)
        self.assertTrue(all(call["permission"] == "plan" and call["runtime_settings"] == {"model": "new", "effort": "high"} for call in later))
        self.assertEqual(sum(event.get("sender") == "user" and event.get("text") == body["text"] for event in final["events"]), 1)
