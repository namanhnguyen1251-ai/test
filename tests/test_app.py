import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from app import State, make_server, lock_data_dir
from meta_client import GraphError


class FakeClient:
    def __init__(self, token, version):
        self.token = token

    def list_pages(self):
        return [{"id": "123", "name": "Page có quyền", "access_token": "private-page-token", "tasks": ["CREATE_CONTENT"]},
                {"id": "456", "name": "Page đọc", "access_token": "private-page-token-2", "tasks": ["ANALYZE"]}]

    def publish(self, page, kind, message, media, title=""):
        return {"id": "123_789", "status": "published"}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(self.folder.name, client_factory=FakeClient)

    def tearDown(self):
        self.state.close()
        self.folder.cleanup()

    def payload(self, **kwargs):
        return {"page_ids": ["123"], "kind": "text", "message": "Nội dung", "delay_seconds": 1, **kwargs}

    def wait(self):
        self.state.worker.join(timeout=10)
        self.assertFalse(self.state.worker.is_alive())

    def test_credentials_never_returned_or_stored(self):
        self.state.connect("private-user-token", "v23.0")
        self.state.begin_job(self.payload())
        self.wait()
        public = json.dumps(self.state.bootstrap())
        database = self.state.db_path.read_bytes()
        for token in ("private-user-token", "private-page-token"):
            self.assertNotIn(token, public)
            self.assertNotIn(token.encode(), database)
        self.assertTrue(self.state.public_pages()[0]["can_publish"])
        self.assertFalse(self.state.public_pages()[1]["can_publish"])

    def test_duplicates_and_unknown_pages_are_rejected(self):
        self.state.connect("private-user-token", "v23.0")
        for ids in (["123", "123"], ["not-owned"], ["456"], []):
            with self.assertRaises(ValueError):
                self.state.begin_job(self.payload(page_ids=ids))

    def test_repeated_submission_id_does_not_post_twice(self):
        class Counting(FakeClient):
            calls = 0

            def publish(client, *args, **kwargs):
                client.calls += 1
                return {"id": "123_789", "status": "published"}

        self.state.client_factory = Counting
        self.state.connect("private-user-token", "v23.0")
        payload = self.payload(request_id="browser-operation-unique-123")
        self.state.begin_job(payload)
        self.wait()
        duplicate = self.state.begin_job(payload)
        self.assertTrue(duplicate["reused"])
        self.assertEqual(self.state.client.calls, 1)
        self.state.job = None
        restored = self.state.begin_job(payload)
        self.assertTrue(restored["reused"])
        self.assertEqual(self.state.client.calls, 1)

    def test_payload_and_media_validation(self):
        self.state.connect("private-user-token", "v23.0")
        for values in ({"kind": "reel"}, {"kind": "photos"}, {"kind": "video"}, {"message": " "},
                       {"delay_seconds": 0}, {"delay_seconds": True}, {"media_ids": ["missing"]}):
            with self.assertRaises(ValueError):
                self.state.begin_job(self.payload(**values))

    def test_uncertain_publish_is_not_retried(self):
        class Failing(FakeClient):
            calls = 0

            def publish(client, *args, **kwargs):
                client.calls += 1
                raise GraphError("Timeout; kiểm tra Page", uncertain=True)

        self.state.client_factory = Failing
        self.state.connect("private-user-token", "v23.0")
        self.state.begin_job(self.payload())
        self.wait()
        self.assertEqual(self.state.client.calls, 1)
        self.assertEqual(self.state.job["results"][0]["state"], "uncertain")

    def test_stop_finishes_current_and_cancels_pending(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(FakeClient):
            calls = 0

            def publish(client, *args, **kwargs):
                client.calls += 1
                entered.set()
                release.wait(timeout=5)
                return {"id": "123_789", "status": "published"}

        self.state.client_factory = Slow
        self.state.connect("private-user-token", "v23.0")
        self.state.pages.append({"id": "789", "name": "Page 2", "access_token": "secret", "tasks": ["CREATE_CONTENT"]})
        self.state.begin_job(self.payload(page_ids=["123", "789"]))
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(ValueError):
                self.state.disconnect()
            with self.assertRaises(ValueError):
                self.state.begin_job(self.payload())
            self.state.stop.set()
        finally:
            release.set()
        self.wait()
        self.assertEqual(self.state.client.calls, 1)
        self.assertEqual([r["state"] for r in self.state.job["results"]], ["published", "cancelled"])
        self.assertEqual(self.state.job["state"], "stopped")

    def test_demo_never_constructs_real_client(self):
        self.state.client_factory = lambda *args: self.fail("Demo called real Meta")
        self.state.connect_demo()
        self.assertEqual(len(self.state.pages), 50)
        self.state.begin_job(self.payload(page_ids=[self.state.pages[0]["id"]]))
        self.wait()
        self.assertTrue(self.state.job["demo"])
        self.assertTrue(self.state.job["results"][0]["post_id"].startswith("demo_"))

    def test_interrupted_history_is_not_automatically_reposted(self):
        job = {"id": "old", "state": "running", "completed": 0, "results": [{"state": "running"}, {"state": "queued"}]}
        with sqlite3.connect(self.state.db_path) as db:
            db.execute("INSERT INTO jobs VALUES (?,?)", ("old", json.dumps(job)))
        recovered = State(self.folder.name, client_factory=FakeClient)
        try:
            self.assertIsNone(recovered.job)
            history = recovered.history()[0]
            self.assertEqual(history["state"], "stopped")
            self.assertEqual([r["state"] for r in history["results"]], ["uncertain", "cancelled"])
        finally:
            recovered.close()

    def test_two_instances_cannot_share_data_directory(self):
        first = lock_data_dir(self.folder.name)
        try:
            with self.assertRaises(OSError):
                lock_data_dir(self.folder.name)
        finally:
            first.close()
        second = lock_data_dir(self.folder.name)
        second.close()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.state = State(cls.folder.name, client_factory=FakeClient)
        cls.server = make_server(0, cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.state.close()
        cls.folder.cleanup()

    def call(self, path, data=None, headers=None, method=None):
        request = Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers
        except HTTPError as exc:
            return exc.code, json.loads(exc.read()), exc.headers

    def json_post(self, path, payload, extra_headers=None):
        return self.call(path, json.dumps(payload).encode(), {"Content-Type": "application/json",
                         "X-CSRF-Token": self.state.csrf, **(extra_headers or {})})

    def test_missing_csrf_blocked(self):
        code, _, _ = self.call("/api/demo", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(code, 403)

    def test_foreign_origin_and_dns_rebinding_blocked(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            code, _, _ = self.json_post("/api/demo", {}, headers)
            self.assertEqual(code, 403)

    def test_demo_bootstrap_no_token(self):
        code, result, _ = self.json_post("/api/demo", {})
        self.assertEqual(code, 200)
        self.assertEqual(len(result["pages"]), 50)
        code, payload, headers = self.call("/api/bootstrap")
        self.assertTrue(payload["demo"])
        self.assertNotIn("demo-only", json.dumps(payload))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_upload_rejects_disguised_binary(self):
        code, _, _ = self.call("/api/media", b"not-a-photo", {"X-CSRF-Token": self.state.csrf,
                             "X-Filename": "picture.jpg", "Content-Type": "image/jpeg"})
        self.assertEqual(code, 400)

    def test_upload_stores_only_session_file_not_path_in_response(self):
        code, result, _ = self.call("/api/media", b"\x89PNG\r\n\x1a\nexample", {"X-CSRF-Token": self.state.csrf,
                                   "X-Filename": "photo.png", "Content-Type": "image/png"})
        self.assertEqual(code, 200)
        self.assertNotIn("path", result)
        self.assertEqual(result["content_type"], "image/png")
        code, result, _ = self.call("/api/media/" + result["id"], headers={"X-CSRF-Token": self.state.csrf}, method="DELETE")
        self.assertTrue(result["deleted"])


if __name__ == "__main__":
    unittest.main()
