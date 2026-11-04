"""Accepted media survives a real HTTP technical-failure carry; no models."""
import base64
import hashlib
from pathlib import Path
import unittest

import test_native_review_submission_http as review
import test_room_media_http as media


class CarriedDeliveryMediaHTTPTests(unittest.TestCase):
    request = review.NativeReviewSubmissionHTTPTests.request
    tearDown = review.NativeReviewSubmissionHTTPTests.tearDown
    create = review.NativeReviewSubmissionHTTPTests.create
    settled = review.NativeReviewSubmissionHTTPTests.settled
    handler = review.NativeReviewSubmissionHTTPTests.handler
    continue_body = staticmethod(review.NativeReviewSubmissionHTTPTests.continue_body)
    job_rows = review.NativeReviewSubmissionHTTPTests.job_rows
    legacy_failed_and_carried = review.NativeReviewSubmissionHTTPTests.legacy_failed_and_carried
    assets = media.RoomMediaHTTPTests.assets
    resolve = media.RoomMediaHTTPTests.resolve
    access = media.RoomMediaHTTPTests.access
    resource = media.RoomMediaHTTPTests.resource

    def setUp(self):
        review.NativeReviewSubmissionHTTPTests.setUp(self)
        directory = self.root / "external deliveries"
        directory.mkdir()
        self.video_path = directory / "示例慢放.mp4"
        self.image_path = directory / "结果 截图.png"
        # The default video is deliberately fake. Browser validation may pass
        # a genuine H264 source through bootstrap, without calling a model.
        self.video_path.write_bytes(b"\x00\xffsynthetic MP4 transport fixture" * 100)
        self.image_path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII="))

    async def four_task_handler(self, agent, context, cwd):
        reply = await review.NativeReviewSubmissionHTTPTests.four_task_handler(self, agent, context, cwd)
        phase = context["job"]["job_phase"]
        if phase == "work" and context["task"]["id"] == "T1":
            for command in reply["commands"]:
                if command["type"] == "complete":
                    command["result"] = (f"The report references video <{self.video_path}> "
                                         f"and image <{self.image_path}>. Byte transport alone is not a playback claim.")
        elif phase == "checkpoint":
            for command in reply["commands"]:
                if command["type"] == "checkpoint":
                    command["summary"] = (f"原任务的报告与汇总已互审完成。录像 <{self.video_path}>；"
                                          f"图片 <{self.image_path}>。此前技术失败已通过新审核继续，旧失败账保留。")
        return reply

    def bootstrap(self, *, video_source=None, image_source=None):
        """Return a completed carried room; keep this fixture alive for browser use."""
        for source, destination in ((video_source, self.video_path), (image_source, self.image_path)):
            if source is not None:
                destination.write_bytes(Path(source).read_bytes())
        failed, carried = self.legacy_failed_and_carried()
        self.original_run_id = failed["current_run"]["id"]
        room_id = carried["room"]["id"]
        old_ids = [job["id"] for job in carried["jobs"]]
        old_rows = self.job_rows(old_ids)
        previous_seq = max(event["seq"] for event in carried["events"])
        code, response = self.request("POST", f"/api/rooms/{room_id}/messages", self.continue_body(carried))
        self.assertEqual(code, 200, {"error": response.get("error")})
        final = self.settled(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"]["stop_reason"])
        self.assertEqual(self.job_rows(old_ids), old_rows)
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1, "T4": 1})
        self.continue_seq = max(event["seq"] for event in final["events"]
                                if event["seq"] > previous_seq and event.get("sender") == "user"
                                and event.get("text") == "继续")
        self.assertEqual(self.service.store.room_attachments(room_id), [])
        return final

    def test_carried_accepted_media_keeps_original_source_and_current_delivery(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        self.assertNotEqual(final["current_run"]["id"], self.original_run_id)
        assets = self.assets(room_id)
        for path, kind in ((self.video_path, "video"), (self.image_path, "image")):
            with self.subTest(name=path.name):
                raw = path.read_bytes()
                code, resolved = self.resolve(room_id, str(path))
                self.assertEqual(code, 200, {"error": resolved.get("error")})
                asset = resolved["asset"]
                self.assertIn(asset["id"], {item["id"] for item in assets})
                self.assertEqual((asset["sha256"], asset["kind"]), (hashlib.sha256(raw).hexdigest(), kind))
                self.assertEqual(asset["source"]["run_id"], self.original_run_id)
                delivery = asset["source"]["delivery"]
                self.assertEqual(delivery["run_id"], final["current_run"]["id"])
                self.assertGreater(delivery["seq"], self.continue_seq)
                event = next(event for event in final["events"] if event["seq"] == delivery["seq"])
                self.assertEqual((event["kind"], event["decision"]), ("objective_checkpoint_finished", "complete"))
                ticket = self.access(room_id, asset)
                self.assertEqual(self.resource(ticket["url"])[::2], (200, raw))
                status, headers, data = self.resource(ticket["url"], headers={"Range": "bytes=1-7"})
                self.assertEqual((status, data), (206, raw[1:8]))
                self.assertEqual(headers["content-range"], f"bytes 1-7/{len(raw)}")


if __name__ == "__main__":
    unittest.main()
