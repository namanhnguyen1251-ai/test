import copy
import threading
import unittest

from comment_monitor import CommentMonitor
from meta_client import GraphError


class FakeClient:
    user_token = "private-user-token"

    def __init__(self):
        self.checks = []
        self.comments = []
        self.status = {"id": "123_789", "status": "published"}
        self.check_error = None
        self.comment_error = None
        self.on_comment = None

    def check_publication(self, page, post_id, kind):
        self.checks.append((page["id"], post_id, kind))
        if self.check_error:
            raise self.check_error
        return copy.deepcopy(self.status)

    def comment(self, page, post_id, message):
        self.comments.append((page["id"], post_id, message))
        if self.on_comment:
            self.on_comment()
        if self.comment_error:
            raise self.comment_error
        return {"id": "123_789_777", "status": "published"}


class FakeState:
    """A fake durable callback with the same atomic compare-and-patch contract."""

    def __init__(self, result=None):
        self.lock = threading.RLock()
        self.api_lock = threading.RLock()
        self.shutdown = threading.Event()
        self.client = FakeClient()
        self.pages = [{"id": "123", "name": "Page", "access_token": "private-page-token"}]
        self.demo = False
        self.connecting = False
        self.jobs = [{"id": "job1", "demo": False, "posts": [], "results": [result or row()]}]
        self.patches = []
        self.before_update = None

    def plans(self):
        with self.lock:
            return copy.deepcopy(self.jobs)

    def update_result(self, job_id, index, patch, expected=None):
        with self.lock:
            if self.before_update:
                self.before_update(job_id, index, patch)
            job = next((job for job in self.jobs if job["id"] == job_id), None)
            if job is None or not 0 <= index < len(job["results"]):
                return False
            result = job["results"][index]
            if expected and any(result.get(key) != value for key, value in expected.items()):
                return False
            result.update(copy.deepcopy(patch))
            self.patches.append(copy.deepcopy(patch))
            return True

    @property
    def result(self):
        return self.jobs[0]["results"][0]


def row(**overrides):
    return {
        "page_id": "123", "page_name": "Page", "post_index": 0,
        "kind": "text", "state": "scheduled", "post_id": "123_789",
        "scheduled_at": 1000, "published_at": None,
        "comment_message": "Bình luận của Page", "comment_delay_seconds": 0,
        "comment_state": "waiting_post", "comment_due_at": None,
        "comment_id": None, "error": "", "comment_error": "", **overrides,
    }


class CommentMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000
        self.state = FakeState()
        self.monitor = CommentMonitor(self.state, clock=lambda: self.clock)

    def tearDown(self):
        self.monitor.stop()

    def test_native_schedule_is_not_polled_or_commented_before_due(self):
        self.clock = 999
        self.monitor.tick()
        self.assertEqual(self.state.client.checks, [])
        self.assertEqual(self.state.client.comments, [])
        self.assertEqual(self.state.result["state"], "scheduled")

    def test_clock_is_not_publication_proof(self):
        self.state.client.status = {"status": "processing"}
        self.monitor.tick()
        self.assertEqual(len(self.state.client.checks), 1)
        self.assertEqual(self.state.client.comments, [])
        self.assertEqual(self.state.result["state"], "scheduled")
        self.assertEqual(self.state.result["comment_state"], "waiting_post")

    def test_explicit_publication_can_trigger_exactly_one_comment(self):
        self.monitor.tick()
        self.monitor.tick()
        self.assertEqual(len(self.state.client.checks), 1)
        self.assertEqual(self.state.client.comments, [("123", "123_789", "Bình luận của Page")])
        self.assertEqual(self.state.result["state"], "published")
        self.assertEqual(self.state.result["comment_state"], "commented")
        self.assertEqual(self.state.result["comment_id"], "123_789_777")

    def test_delayed_comment_uses_observed_publication_time(self):
        self.state.result["comment_delay_seconds"] = 30
        self.clock = 1020
        self.monitor.tick()
        self.assertEqual(self.state.result["published_at"], 1020)
        self.assertEqual(self.state.result["comment_due_at"], 1050)
        self.clock = 1049
        self.monitor.tick()
        self.assertEqual(self.state.client.comments, [])
        self.assertEqual(self.state.result["comment_due_at"], 1050)
        self.clock = 1050
        self.monitor.tick()
        self.assertEqual(len(self.state.client.comments), 1)

    def test_immediate_published_comment_uses_saved_time_after_restart(self):
        self.state.jobs[0]["results"][0] = row(
            state="published", published_at=900, comment_state="waiting_connection",
            comment_delay_seconds=20,
        )
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_due_at"], 920)
        self.assertEqual(len(self.state.client.comments), 1)
        self.assertEqual(self.state.client.checks, [])

    def test_video_processing_is_polled_without_scheduled_time(self):
        self.state.jobs[0]["results"][0] = row(kind="video", state="processing", scheduled_at=None)
        self.state.client.status = {"status": "processing"}
        self.monitor.tick()
        self.assertEqual(self.state.client.comments, [])
        self.state.client.status = {"status": "published"}
        self.monitor.tick()
        self.assertEqual(len(self.state.client.checks), 2)
        self.assertEqual(self.state.client.checks[0][2], "video")
        self.assertEqual(len(self.state.client.comments), 1)

    def test_disconnect_preserves_native_schedule_and_reconnects(self):
        client = self.state.client
        self.state.client = None
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "scheduled")
        self.assertEqual(self.state.result["comment_state"], "waiting_connection")
        self.assertEqual(client.checks, [])
        self.state.client = client
        self.monitor.tick()
        self.monitor.tick()
        self.assertEqual(len(client.checks), 1)
        self.assertEqual(len(client.comments), 1)

    def test_future_schedule_restores_waiting_post_after_reconnection(self):
        self.clock = 999
        self.state.result["comment_state"] = "waiting_connection"
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_state"], "waiting_post")
        self.assertEqual(self.state.client.checks, [])

    def test_missing_page_or_connecting_never_uses_client(self):
        self.state.pages = []
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_state"], "waiting_connection")
        self.state.pages = [{"id": "123", "access_token": "private-page-token"}]
        self.state.connecting = True
        self.monitor.tick()
        self.assertEqual(self.state.client.checks, [])
        self.assertEqual(self.state.client.comments, [])

    def test_wrong_real_demo_session_never_writes(self):
        for job_demo, session_demo in ((True, False), (False, True)):
            with self.subTest(job_demo=job_demo, session_demo=session_demo):
                self.state.jobs[0]["demo"] = job_demo
                self.state.demo = session_demo
                self.state.result.update(state="scheduled", comment_state="waiting_post")
                self.monitor.tick()
                self.assertEqual(self.state.result["comment_state"], "waiting_connection")
        self.assertEqual(self.state.client.checks, [])
        self.assertEqual(self.state.client.comments, [])

    def test_demo_can_simulate_only_after_due_and_delay(self):
        client = self.state.client
        self.state.jobs[0]["demo"] = True
        self.state.demo = True
        self.state.client = None
        self.state.result["comment_delay_seconds"] = 5
        self.clock = 999
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "scheduled")
        self.clock = 1000
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "published")
        self.assertEqual(self.state.result["comment_state"], "waiting_delay")
        self.clock = 1005
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_state"], "commented")
        self.assertTrue(self.state.result["comment_id"].startswith("demo-comment-"))
        self.assertEqual(client.checks, [])
        self.assertEqual(client.comments, [])

    def test_status_error_keeps_post_pending_and_redacts_tokens(self):
        self.state.client.status = {"status": "processing", "error": "private-page-token private-user-token"}
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "scheduled")
        self.assertNotIn("private", self.state.result["publication_error"])
        self.assertEqual(self.state.client.comments, [])
        self.state.client.check_error = GraphError("Temporary read error", uncertain=True)
        self.monitor.tick()
        self.assertEqual(self.state.result["publication_error"], "Temporary read error")
        self.assertEqual(len(self.state.client.checks), 2)

    def test_explicit_failed_publication_cancels_comment(self):
        self.state.client.status = {"status": "failed", "error": "Video rejected"}
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "failed")
        self.assertEqual(self.state.result["comment_state"], "cancelled")
        self.assertEqual(self.state.result["error"], "Video rejected")
        self.assertEqual(self.state.client.comments, [])

    def test_ambiguous_comment_is_persisted_and_never_retried(self):
        self.state.client.comment_error = GraphError("Timeout private-page-token", uncertain=True)
        self.monitor.tick()
        self.monitor.tick()
        self.assertEqual(len(self.state.client.comments), 1)
        self.assertEqual(self.state.result["comment_state"], "uncertain")
        self.assertNotIn("private-page-token", self.state.result["comment_error"])

    def test_known_comment_failure_and_unexpected_failure_are_not_retried(self):
        for failure, expected in ((GraphError("Permission missing"), "failed"), (RuntimeError("private-page-token"), "uncertain")):
            with self.subTest(expected=expected):
                self.state.jobs[0]["results"][0] = row()
                self.state.client.comments.clear()
                self.state.client.comment_error = failure
                self.monitor.tick()
                self.monitor.tick()
                self.assertEqual(len(self.state.client.comments), 1)
                self.assertEqual(self.state.result["comment_state"], expected)
                self.assertNotIn("private-page-token", self.state.result["comment_error"])

    def test_sending_is_durable_before_network_and_busy_is_exposed(self):
        def inspect_claim():
            self.assertEqual(self.state.result["comment_state"], "sending")
            self.assertTrue(self.monitor.busy())
            self.assertTrue(self.monitor.active_write)
            self.assertTrue(any(patch.get("comment_state") == "sending" for patch in self.state.patches))
        self.state.client.on_comment = inspect_claim
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_state"], "commented")
        self.assertFalse(self.monitor.busy())
        self.assertFalse(self.monitor.active_write)

    def test_interrupted_or_terminal_comments_are_never_retried(self):
        for comment_state in ("sending", "commented", "failed", "uncertain", "cancelled"):
            with self.subTest(comment_state=comment_state):
                self.state.jobs[0]["results"][0] = row(state="published", comment_state=comment_state)
                self.monitor.tick()
                self.assertEqual(self.state.result["comment_state"], comment_state)
        self.assertEqual(self.state.client.comments, [])

    def test_unknown_or_unpublished_states_never_comment(self):
        for post_state in ("queued", "running", "failed", "uncertain", "cancelled", "unknown"):
            with self.subTest(post_state=post_state):
                self.state.jobs[0]["results"][0] = row(state=post_state)
                self.monitor.tick()
        self.assertEqual(self.state.client.checks, [])
        self.assertEqual(self.state.client.comments, [])

    def test_cancelled_comment_claim_prevents_post(self):
        def cancel(job_id, index, patch):
            if patch.get("comment_state") == "sending":
                self.state.result["comment_state"] = "cancelled"
        self.state.before_update = cancel
        self.monitor.tick()
        self.assertEqual(self.state.result["comment_state"], "cancelled")
        self.assertEqual(self.state.client.comments, [])

    def test_connection_is_rechecked_after_waiting_for_shared_api_lock(self):
        acquired, release = threading.Event(), threading.Event()
        original_context = self.monitor._api_context
        class Gate:
            def __enter__(gate):
                acquired.set()
                release.wait(2)
            def __exit__(gate, *args):
                pass
        self.monitor._api_context = lambda: Gate()
        thread = threading.Thread(target=self.monitor.tick)
        thread.start()
        self.assertTrue(acquired.wait(2))
        client = self.state.client
        with self.state.lock:
            self.state.client = None
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(client.checks, [])
        self.assertEqual(client.comments, [])
        self.assertEqual(self.state.result["comment_state"], "waiting_connection")
        self.monitor._api_context = original_context

    def test_concurrent_ticks_do_not_send_duplicate_comments(self):
        entered, release = threading.Event(), threading.Event()
        self.state.client.on_comment = lambda: (entered.set(), release.wait(2))
        thread = threading.Thread(target=self.monitor.tick)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.monitor.tick()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.monitor.tick()
        self.assertEqual(len(self.state.client.comments), 1)

    def test_native_publication_is_observed_without_a_comment(self):
        self.state.result.update(comment_message="", comment_state="")
        self.monitor.tick()
        self.assertEqual(self.state.result["state"], "published")
        self.assertEqual(len(self.state.client.checks), 1)
        self.assertEqual(self.state.client.comments, [])

    def test_legacy_results_are_preserved(self):
        del self.state.result["comment_state"]
        self.monitor.tick()
        self.assertEqual(self.state.client.checks, [])
        self.assertEqual(self.state.client.comments, [])

    def test_all_posts_and_pages_are_monitored_independently(self):
        self.state.jobs[0]["results"].append(row(post_index=1, post_id="123_790"))
        self.state.jobs.append({"id": "job2", "demo": False, "results": [row(post_id="123_791")]})
        self.monitor.tick()
        self.monitor.tick()
        self.assertEqual([item[1] for item in self.state.client.comments], ["123_789", "123_790", "123_791"])

    def test_thread_start_is_idempotent_and_shutdown_stops_scans(self):
        self.monitor.start()
        thread = self.monitor.thread
        self.monitor.start()
        self.assertIs(self.monitor.thread, thread)
        self.assertTrue(thread.daemon)
        self.state.shutdown.set()
        self.monitor.stop()
        self.assertFalse(thread.is_alive())
        count = len(self.state.client.checks)
        self.monitor.tick()
        self.assertEqual(len(self.state.client.checks), count)


if __name__ == "__main__":
    unittest.main()
