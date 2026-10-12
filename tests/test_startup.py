import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


class StartupTests(unittest.TestCase):
    def test_real_entrypoint_serves_and_runs_comment_monitor(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            logfile = Path(folder) / "server.log"
            with logfile.open("wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-u", "app.py", "--no-browser", "--port", "0", "--data-dir", str(Path(folder) / "data")],
                    cwd=root, stdout=output, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                try:
                    deadline = time.monotonic() + 8
                    address = None
                    while time.monotonic() < deadline:
                        log = logfile.read_text(encoding="utf-8", errors="replace")
                        match = re.search(r"http://127\.0\.0\.1:\d+", log)
                        if match:
                            address = match[0]
                            break
                        if process.poll() is not None:
                            self.fail("App entrypoint exited before readiness: " + log)
                        time.sleep(0.05)
                    self.assertIsNotNone(address, logfile.read_text(encoding="utf-8", errors="replace"))
                    opener = build_opener(ProxyHandler({}))

                    def get(path):
                        with opener.open(address + path, timeout=3) as response:
                            return json.load(response)

                    session = get("/api/bootstrap")
                    self.assertFalse(session["connected"])
                    self.assertEqual(get("/api/plans")["jobs"], [])

                    def post(path, data):
                        request = Request(address + path, data=json.dumps(data).encode(), headers={
                            "Content-Type": "application/json", "X-CSRF-Token": session["csrf_token"]})
                        with opener.open(request, timeout=3) as response:
                            return json.load(response)

                    page = post("/api/demo", {})["pages"][0]
                    job = post("/api/jobs", {"page_ids": [page["id"]], "kind": "text", "message": "Entry-point demo",
                                             "comment_message": "Demo comment", "delay_seconds": 1})["job"]
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        saved = get("/api/jobs/" + job["id"])["job"]
                        if saved["results"][0].get("comment_state") == "commented":
                            break
                        time.sleep(0.1)
                    self.assertEqual(saved["results"][0]["state"], "published")
                    self.assertEqual(saved["results"][0]["comment_state"], "commented")
                    self.assertTrue(saved["results"][0]["comment_id"].startswith("demo-comment-"))
                finally:
                    if process.poll() is None:
                        if os.name == "nt":
                            process.terminate()
                        else:
                            process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
