import json
import tempfile
import threading
import time
import unittest

from app import State
from comment_monitor import CommentMonitor
from meta_client import GraphError


class PlanningClient:
    def __init__(self, token, version):
        self.user_token = token
        self.calls = []
        self.comments = []
        self.ready = False

    def list_pages(self):
        return [{"id": "123", "name": "Trang thử", "access_token": "private-page-key", "tasks": ["CREATE_CONTENT"]}]

    def publish(self, page, kind, message, media, title="", scheduled_at=None):
        self.calls.append({"page_id": page["id"], "kind": kind, "message": message, "scheduled_at": scheduled_at})
        return {"id": f"{page['id']}_{1000 + len(self.calls)}", "status": "scheduled" if scheduled_at is not None else "published"}

    def check_publication(self, page, post_id, kind):
        return {"id": post_id, "status": "published" if self.ready else "processing"}

    def comment(self, page, post_id, message):
        self.comments.append((page["id"], post_id, message))
        return {"id": "123_5000", "status": "published"}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(self.folder.name, client_factory=PlanningClient)
        self.state.connect("private-user-key", "v24.0")

    def tearDown(self):
        self.state.close()
        self.folder.cleanup()

    def post(self, **changes):
        return {"page_ids": ["123"], "kind": "text", "message": "Bài viết", "media_ids": [], **changes}

    def start(self, posts):
        result = self.state.begin_job({"request_id": "unique-request-for-test-123", "posts": posts, "delay_seconds": 1})
        self.state.worker.join(timeout=8)
        self.assertFalse(self.state.worker.is_alive())
        return result

    def test_two_distinct_posts_per_page_have_separate_native_schedules(self):
        schedule = int(time.time()) + 3600
        self.start([self.post(message="Bài một", scheduled_at=schedule), self.post(message="Bài hai", scheduled_at=schedule + 3600)])
        job = self.state.job
        self.assertEqual(job["total"], 2)
        self.assertEqual(job["post_count"], 2)
        self.assertEqual([c["message"] for c in self.state.client.calls], ["Bài một", "Bài hai"])
        self.assertEqual([c["scheduled_at"] for c in self.state.client.calls], [schedule, schedule + 3600])
        self.assertEqual([r["state"] for r in job["results"]], ["scheduled", "scheduled"])
        self.assertNotEqual(job["results"][0]["post_id"], job["results"][1]["post_id"])
        self.assertEqual(self.state.client.comments, [])

    def test_entire_list_validated_before_first_write(self):
        with self.assertRaises(ValueError):
            self.start([self.post(message="Hợp lệ"), self.post(message=" ")])
        self.assertEqual(self.state.client.calls, [])
        self.assertIsNone(self.state.worker)

    def test_schedule_invalid_or_outside_window_never_becomes_immediate(self):
        current = int(time.time())
        for value in [True, 0, current - 60, current + 1199, current + 29 * 86400, "tomorrow", float(current + 3600)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.state.begin_job(self.post(scheduled_at=value))
        self.assertEqual(self.state.client.calls, [])

    def test_comment_limits_validated_without_any_writes(self):
        for changes in [{"comment_message": "x" * 8001}, {"comment_delay_seconds": -1}, {"comment_delay_seconds": 3601},
                        {"comment_delay_seconds": True}, {"comment_message": ["invalid"]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.begin_job(self.post(**changes))
        self.assertEqual(self.state.client.calls, [])

    def test_native_ids_survive_restart_and_comments_wait_for_explicit_publication(self):
        schedule = int(time.time()) + 3600
        self.start([self.post(scheduled_at=schedule, comment_message="Bình luận sau đăng")])
        original_id = self.state.job["results"][0]["post_id"]
        self.state.close()
        recovered = State(self.folder.name, client_factory=PlanningClient)
        monitor = CommentMonitor(recovered, clock=lambda: schedule + 1)
        try:
            self.assertIsNone(recovered.job)
            monitor.tick()
            result = recovered.plans()[0]["results"][0]
            self.assertEqual(result["state"], "scheduled")
            self.assertEqual(result["post_id"], original_id)
            self.assertEqual(result["comment_state"], "waiting_connection")
            recovered.connect("private-user-key", "v24.0")
            monitor.tick()
            self.assertEqual(recovered.client.comments, [])
            self.assertEqual(recovered.plans()[0]["results"][0]["state"], "scheduled")
            recovered.client.ready = True
            monitor.tick()
            self.assertEqual(recovered.client.comments, [("123", original_id, "Bình luận sau đăng")])
            monitor.tick()
            self.assertEqual(len(recovered.client.comments), 1)
            self.assertEqual(recovered.plans()[0]["results"][0]["comment_state"], "commented")
            encoded = json.dumps(recovered.plans())
            for secret in ["private-user-key", "private-page-key"]:
                self.assertNotIn(secret, encoded)
                self.assertNotIn(secret.encode(), recovered.db_path.read_bytes())
        finally:
            monitor.stop()
            recovered.close()

    def test_comment_cancellation_during_publish_is_not_resurrected(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(PlanningClient):
            def publish(client, *args, **kwargs):
                entered.set()
                release.wait(5)
                return super().publish(*args, **kwargs)

        self.state.client_factory = Blocking
        self.state.connect("private-user-key", "v24.0")
        self.state.begin_job(self.post(comment_message="Không gửi bình luận này"))
        self.assertTrue(entered.wait(2))
        try:
            self.state.cancel_comment(self.state.job["id"], 0)
        finally:
            release.set()
        self.state.worker.join(5)
        monitor = CommentMonitor(self.state)
        monitor.tick()
        self.assertEqual(self.state.job["results"][0]["state"], "published")
        self.assertEqual(self.state.job["results"][0]["comment_state"], "cancelled")
        self.assertEqual(self.state.client.comments, [])

    def test_cancelled_delayed_comment_is_durable(self):
        self.start([self.post(comment_message="Bình luận", comment_delay_seconds=3600)])
        job_id = self.state.job["id"]
        response = self.state.cancel_comment(job_id, 0)
        self.assertEqual(response["job"]["results"][0]["comment_state"], "cancelled")
        CommentMonitor(self.state, clock=lambda: time.time() + 4000).tick()
        self.assertEqual(self.state.client.comments, [])

    def test_interrupted_comment_is_uncertain_and_never_retried(self):
        self.start([self.post(comment_message="Bình luận")])
        self.state.update_result(self.state.job["id"], 0, {"comment_state": "sending"})
        self.state.close()
        recovered = State(self.folder.name, client_factory=PlanningClient)
        try:
            recovered.connect("private-user-key", "v24.0")
            CommentMonitor(recovered).tick()
            self.assertEqual(recovered.plans()[0]["results"][0]["comment_state"], "uncertain")
            self.assertEqual(recovered.client.comments, [])
        finally:
            recovered.close()

    def test_stop_preserves_schedule_already_accepted_by_meta(self):
        schedule = int(time.time()) + 3600
        entered, release = threading.Event(), threading.Event()

        class Blocking(PlanningClient):
            def publish(client, *args, **kwargs):
                entered.set()
                release.wait(5)
                return super().publish(*args, **kwargs)

        self.state.client_factory = Blocking
        self.state.connect("private-user-key", "v24.0")
        self.state.begin_job({"posts": [self.post(scheduled_at=schedule, comment_message="Có thể bình luận"),
                                       self.post(scheduled_at=schedule + 3600)], "delay_seconds": 1})
        self.assertTrue(entered.wait(2))
        self.state.stop.set()
        release.set()
        self.state.worker.join(5)
        self.assertEqual([r["state"] for r in self.state.job["results"]], ["scheduled", "cancelled"])
        self.assertEqual(self.state.job["results"][0]["comment_state"], "waiting_post")
        self.assertEqual(len(self.state.client.calls), 1)

    def test_plans_retain_old_future_schedule_beyond_recent_history(self):
        pending = {"id": "old-pending", "state": "completed", "results": [{"state": "scheduled", "comment_state": ""}]}
        self.state._store_job(pending)
        for index in range(25):
            self.state._store_job({"id": str(index), "state": "completed", "results": [{"state": "published", "comment_state": ""}]})
        self.assertEqual(len(self.state.plans()), 21)
        self.assertIn("old-pending", [job["id"] for job in self.state.plans()])


if __name__ == "__main__":
    unittest.main()
