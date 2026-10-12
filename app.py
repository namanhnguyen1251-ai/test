"""PagePilot: a local, explicit-review publishing queue for managed Facebook Pages."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from meta_client import GraphClient, GraphError

ROOT = Path(__file__).resolve().parent
MAX_VIDEO = 1024 * 1024 * 1024
MAX_PHOTO = 20 * 1024 * 1024
FINISHED = {"scheduled", "published", "processing", "failed", "uncertain", "cancelled"}
PENDING_COMMENTS = {"waiting_post", "waiting_delay", "waiting_connection", "sending"}


def now():
    return datetime.now(timezone.utc).isoformat()


def default_data_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PagePilot"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "PagePilot"


def lock_data_dir(data_dir):
    """Keep two application instances from mutating the same publishing history."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / ".instance.lock").open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise OSError("Ứng dụng đang chạy với thư mục dữ liệu này.") from None
    return handle


class State:
    def __init__(self, data_dir, client_factory=GraphClient):
        self.lock = threading.RLock()
        self.api_lock = threading.RLock()
        self.monitor = None
        self.analytics = None
        self.session_revision = 0
        self.client_factory = client_factory
        self.csrf = secrets.token_urlsafe(32)
        self.client = None
        self.pages = []
        self.demo = False
        self.graph_version = ""
        self.job = None
        self.media = {}
        self.stop = threading.Event()
        self.shutdown = threading.Event()
        self.worker = None
        self.connecting = False
        self.temp = tempfile.TemporaryDirectory(prefix="pagepilot-")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "history.sqlite3"
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            for job_id, payload in db.execute("SELECT id, payload FROM jobs").fetchall():
                job = json.loads(payload)
                changed = False
                if job.get("state") == "running":
                    job["state"] = "stopped"
                    job["finished_at"] = now()
                    for result in job.get("results", []):
                        if result["state"] == "running":
                            result.update(state="uncertain", error="Phiên trước bị đóng khi đang gửi. Kiểm tra Fanpage trước khi gửi lại.")
                        elif result["state"] == "queued":
                            result["state"] = "cancelled"
                    job["completed"] = len(job.get("results", []))
                    changed = True
                for result in job.get("results", []):
                    if result.get("comment_state") == "sending":
                        result.update(comment_state="uncertain", comment_error="Phiên trước bị đóng khi đang gửi bình luận. Kiểm tra bài trước khi bình luận lại.")
                        changed = True
                    elif result.get("comment_state") in PENDING_COMMENTS and result["state"] in {"failed", "uncertain", "cancelled"}:
                        result["comment_state"] = "cancelled"
                        changed = True
                if changed:
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))

    def active(self):
        return self.job is not None and self.job["state"] == "running"

    def api_busy(self):
        return self.active() or self.connecting or (self.monitor is not None and self.monitor.busy())

    def start_monitor(self):
        from comment_monitor import CommentMonitor
        if self.monitor is None:
            self.monitor = CommentMonitor(self)
            self.monitor.start()

    def get_analytics(self):
        with self.lock:
            if self.analytics is None:
                from analytics_service import AnalyticsService
                self.analytics = AnalyticsService(self)
            return self.analytics

    def session_changed(self):
        self.session_revision += 1
        if self.analytics is not None:
            self.analytics.invalidate()

    def public_pages(self):
        return [self.public_page(p) for p in self.pages]

    @staticmethod
    def public_page(page):
        tasks = page.get("tasks", [])
        allowed = {"CREATE_CONTENT", "MANAGE", "PROFILE_PLUS_CREATE_CONTENT", "PROFILE_PLUS_FULL_CONTROL"}
        return {"id": str(page["id"]), "name": page["name"], "tasks": tasks,
                "can_publish": bool(page.get("access_token")) and (not tasks or bool(allowed.intersection(tasks)))}

    def public_media(self):
        return [{k: m[k] for k in ("id", "name", "content_type", "size")} for m in self.media.values()]

    def history(self):
        with sqlite3.connect(self.db_path) as db:
            records = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 20").fetchall()
        return [json.loads(row[0]) for row in records]

    def get_job(self, job_id):
        with self.lock:
            if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id):
                return None
            if self.job and self.job["id"] == job_id:
                return copy.deepcopy(self.job)
            with sqlite3.connect(self.db_path) as db:
                row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def plans(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as db:
                rows = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC").fetchall()
            jobs = [json.loads(row[0]) for row in rows]
            return [j for index, j in enumerate(jobs) if index < 20 or self.job_pending(j)]

    @staticmethod
    def job_pending(job):
        return job.get("state") == "running" or any(
            r.get("state") in {"scheduled", "processing"} or r.get("comment_state") in PENDING_COMMENTS
            for r in job.get("results", [])
        )

    def _store_job(self, job):
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO jobs (id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (job["id"], json.dumps(job, ensure_ascii=False)))

    def save_job(self):
        self._store_job(self.job)

    def update_result(self, job_id, result_index, patch, expected=None):
        with self.lock:
            if self.job and self.job["id"] == job_id:
                job = self.job
            else:
                with sqlite3.connect(self.db_path) as db:
                    row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row:
                    return False
                job = json.loads(row[0])
            if not isinstance(result_index, int) or isinstance(result_index, bool) or not 0 <= result_index < len(job.get("results", [])):
                return False
            result = job["results"][result_index]
            if expected and any(result.get(key) != value for key, value in expected.items()):
                return False
            result.update(patch)
            self._store_job(job)
            return True

    def cancel_comment(self, job_id, result_index):
        with self.lock:
            if not isinstance(job_id, str) or not isinstance(result_index, int) or isinstance(result_index, bool):
                raise ValueError("Bình luận không hợp lệ.")
            with sqlite3.connect(self.db_path) as db:
                row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Không tìm thấy lượt đăng.")
            job = json.loads(row[0])
            if not 0 <= result_index < len(job.get("results", [])):
                raise ValueError("Không tìm thấy bình luận.")
            result = job["results"][result_index]
            if result.get("comment_state") not in PENDING_COMMENTS - {"sending"}:
                raise ValueError("Bình luận đã gửi, đang gửi hoặc không còn chờ.")
            if not self.update_result(job_id, result_index, {"comment_state": "cancelled"}, expected={"comment_state": result["comment_state"]}):
                raise ValueError("Trạng thái vừa thay đổi. Tải lại kết quả.")
            with sqlite3.connect(self.db_path) as db:
                return {"job": json.loads(db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()[0])}

    def bootstrap(self):
        with self.lock:
            return {"csrf_token": self.csrf, "connected": bool(self.pages), "demo": self.demo,
                    "graph_version": self.graph_version, "pages": self.public_pages(),
                    "job": copy.deepcopy(self.job), "media": self.public_media(), "history": self.history(),
                    "session_revision": self.session_revision}

    def connect(self, token, version):
        if not isinstance(token, str) or not 10 <= len(token) <= 8192 or re.search(r"\s", token):
            raise ValueError("Access token không hợp lệ. Dán token của user đã có quyền quản lý Fanpage.")
        if not isinstance(version, str) or not re.fullmatch(r"v[1-9]\d?\.0", version):
            raise ValueError("Nhập phiên bản Graph API đang dùng trong Meta App, ví dụ theo dạng vXX.0.")
        with self.lock:
            if self.api_busy():
                raise ValueError("Chờ lượt đăng, bình luận hoặc thao tác kết nối hiện tại hoàn tất.")
            self.connecting = True
        try:
            client = self.client_factory(token, version)
            with self.api_lock:
                pages = client.list_pages()
            with self.lock:
                self.client, self.pages = client, pages
                self.demo, self.graph_version = False, version
                self.session_changed()
            return {"pages": self.public_pages(), "demo": False}
        finally:
            with self.lock:
                self.connecting = False

    def connect_demo(self):
        with self.lock:
            if self.api_busy():
                raise ValueError("Chờ lượt đăng, bình luận hoặc thao tác kết nối hiện tại hoàn tất.")
            self.client = None
            self.demo = True
            self.graph_version = "demo"
            self.pages = [{"id": str(100000 + i), "name": f"Fanpage thử nghiệm {i:02d}",
                           "access_token": "demo-only", "tasks": ["CREATE_CONTENT"]} for i in range(1, 51)]
            self.session_changed()
            return {"pages": self.public_pages(), "demo": True}

    def disconnect(self):
        with self.lock:
            if self.api_busy():
                raise ValueError("Chờ lượt đăng hoặc bình luận đang gửi hoàn tất trước khi ngắt kết nối.")
            self.client = None
            self.pages = []
            self.demo = False
            self.graph_version = ""
            self.session_changed()
            return {"connected": False}

    def begin_job(self, data):
        with self.lock:
            request_id = data.get("request_id")
            if request_id is not None:
                if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", request_id):
                    raise ValueError("Mã thao tác không hợp lệ. Mở lại bước xác nhận.")
                if self.job and self.job["id"] == request_id:
                    return {"job": copy.deepcopy(self.job), "reused": True}
                with sqlite3.connect(self.db_path) as db:
                    previous = db.execute("SELECT payload FROM jobs WHERE id=?", (request_id,)).fetchone()
                if previous:
                    return {"job": json.loads(previous[0]), "reused": True}
            if self.api_busy():
                raise ValueError("Đã có lượt đăng, bình luận hoặc thao tác kết nối đang chạy.")
            if not self.pages:
                raise ValueError("Kết nối Meta App hoặc bật chế độ dùng thử trước.")
            delay = data.get("delay_seconds", 3)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 1 <= delay <= 60:
                raise ValueError("Khoảng nghỉ giữa các trang phải từ 1 đến 60 giây.")
            requested = data.get("posts", [data])
            if not isinstance(requested, list) or not 1 <= len(requested) <= 100:
                raise ValueError("Mỗi danh sách cần từ 1 đến 100 bài.")
            posts, tasks, results = [], [], []
            post_ids = set()
            for post_index, item in enumerate(requested):
                post, pages, media = self.validate_post(item)
                if post["id"] in post_ids:
                    raise ValueError("Danh sách bài có mã bị lặp.")
                post_ids.add(post["id"])
                posts.append(post)
                for page in pages:
                    tasks.append((page, post, media))
                    results.append({"page_id": str(page["id"]), "page_name": page["name"], "post_index": post_index,
                                    "post_title": post["title"] or post["message"][:50] or f"Bài {post_index + 1}",
                                    "kind": post["kind"], "scheduled_at": post["scheduled_at"], "state": "queued",
                                    "comment_message": post["comment_message"], "comment_delay_seconds": post["comment_delay_seconds"],
                                    "comment_state": "waiting_post" if post["comment_message"] else "", "comment_due_at": None})
            if len(tasks) > 1000:
                raise ValueError("Mỗi danh sách tối đa 1.000 lượt bài × Fanpage.")
            self.stop.clear()
            self.job = {"id": request_id or secrets.token_hex(12), "state": "running", "kind": posts[0]["kind"] if len(posts) == 1 else "batch",
                        "message": posts[0]["message"] if len(posts) == 1 else f"Danh sách {len(posts)} bài", "title": posts[0]["title"],
                        "created_at": now(), "demo": self.demo, "total": len(tasks), "completed": 0,
                        "post_count": len(posts), "posts": posts, "results": results}
            self.save_job()
            self.worker = threading.Thread(target=self.run_job, args=(tasks, delay, self.client, self.demo), daemon=True)
            self.worker.start()
            return {"job": copy.deepcopy(self.job)}

    def validate_post(self, item):
        if not isinstance(item, dict):
            raise ValueError("Bài viết không hợp lệ.")
        ids = item.get("page_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(i, str) for i in ids):
            raise ValueError("Chọn ít nhất một Fanpage hợp lệ.")
        if len(ids) != len(set(ids)):
            raise ValueError("Danh sách Fanpage có mục bị lặp.")
        pages_by_id = {str(p["id"]): p for p in self.pages}
        if any(i not in pages_by_id for i in ids):
            raise ValueError("Có Fanpage không thuộc phiên kết nối này.")
        pages = [pages_by_id[i] for i in ids]
        if any(not self.public_page(p)["can_publish"] for p in pages):
            raise ValueError("Một số Fanpage chưa có quyền tạo nội dung.")
        kind = item.get("kind")
        if kind not in {"text", "photos", "video", "reel"}:
            raise ValueError("Loại nội dung không hợp lệ.")
        message, title = item.get("message", ""), item.get("title", "")
        if not isinstance(message, str) or len(message) > 63206 or not isinstance(title, str) or len(title) > 255:
            raise ValueError("Nội dung hoặc tiêu đề vượt giới hạn của tool.")
        if kind == "text" and not message.strip():
            raise ValueError("Nhập nội dung bài viết.")
        media_ids = item.get("media_ids", [])
        if not isinstance(media_ids, list) or any(not isinstance(m, str) for m in media_ids) or len(media_ids) != len(set(media_ids)):
            raise ValueError("Danh sách tệp không hợp lệ.")
        if any(m not in self.media for m in media_ids):
            raise ValueError("Có tệp không còn trong phiên này. Vui lòng tải lại.")
        media = [self.media[m] for m in media_ids]
        if kind == "text" and media:
            raise ValueError("Bài văn bản không sử dụng tệp đính kèm.")
        if kind == "photos" and (not 1 <= len(media) <= 10 or any(not m["content_type"].startswith("image/") for m in media)):
            raise ValueError("Bài ảnh cần từ 1 đến 10 ảnh JPG/PNG.")
        if kind in {"video", "reel"} and (len(media) != 1 or media[0]["content_type"] != "video/mp4"):
            raise ValueError("Video hoặc Reels cần đúng một tệp MP4.")
        scheduled_at = item.get("scheduled_at")
        if scheduled_at is not None:
            if not isinstance(scheduled_at, int) or isinstance(scheduled_at, bool):
                raise ValueError("Giờ hẹn phải là số giây Unix hợp lệ.")
            difference = scheduled_at - time.time()
            minimum = 5 if self.demo else 20 * 60
            if not minimum <= difference <= 28 * 24 * 3600:
                raise ValueError("Giờ hẹn cần từ 5 giây đến 28 ngày trong demo." if self.demo else "Giờ hẹn cần từ 20 phút đến 28 ngày để đủ thời gian gửi lịch lên Meta.")
        comment = item.get("comment_message", "")
        if not isinstance(comment, str) or len(comment) > 8000:
            raise ValueError("Bình luận tối đa 8.000 ký tự.")
        comment = comment.strip()
        comment_delay = item.get("comment_delay_seconds", 0)
        if isinstance(comment_delay, bool) or not isinstance(comment_delay, int) or not 0 <= comment_delay <= 3600:
            raise ValueError("Độ trễ bình luận cần từ 0 đến 3.600 giây.")
        post_id = item.get("id") or secrets.token_hex(12)
        if not isinstance(post_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", post_id):
            raise ValueError("Mã bài viết không hợp lệ.")
        return {"id": post_id, "page_ids": ids, "kind": kind, "message": message, "title": title,
                "media_ids": media_ids, "scheduled_at": scheduled_at, "comment_message": comment,
                "comment_delay_seconds": comment_delay}, pages, media

    def run_job(self, tasks, delay, client, demo):
        for index, (page, post, media) in enumerate(tasks):
            with self.lock:
                result = self.job["results"][index]
                if self.stop.is_set() or self.shutdown.is_set():
                    result["state"] = "cancelled"
                    if result["comment_message"]:
                        result["comment_state"] = "cancelled"
                    self.job["completed"] += 1
                    self.save_job()
                    continue
                result["state"] = "running"
                self.save_job()
            try:
                if demo:
                    time.sleep(0.15)
                    output = {"id": f"demo_{page['id']}_{self.job['id'][:8]}_{result['post_index']}",
                              "status": "scheduled" if post["scheduled_at"] is not None else "published"}
                else:
                    with self.api_lock:
                        if post["scheduled_at"] is not None and post["scheduled_at"] - time.time() < 600:
                            raise GraphError("Giờ hẹn quá gần sau khi chờ tải tệp. Tạo lịch mới xa hơn; chưa gửi bài này.")
                        kwargs = {"title": post["title"]}
                        if post["scheduled_at"] is not None:
                            kwargs["scheduled_at"] = post["scheduled_at"]
                        output = client.publish(page, post["kind"], post["message"], media, **kwargs)
                with self.lock:
                    result.update(state=output.get("status", "processing"), post_id=str(output.get("id", "")))
                    if result["state"] not in {"scheduled", "published", "processing"}:
                        result["state"] = "processing"
                    if result["state"] == "published":
                        result["published_at"] = time.time()
                        if result["comment_message"] and result.get("comment_state") in PENDING_COMMENTS - {"sending"}:
                            result.update(comment_state="waiting_delay", comment_due_at=result["published_at"] + result["comment_delay_seconds"])
                    if output.get("permalink_url"):
                        result["permalink_url"] = output["permalink_url"]
            except GraphError as exc:
                with self.lock:
                    result.update(state="uncertain" if getattr(exc, "uncertain", False) else "failed", error=str(exc))
            except Exception:
                # An unexpected exception may follow a successful remote write. Never blindly repost.
                with self.lock:
                    result.update(state="uncertain", error="Không xác định được kết quả. Kiểm tra Fanpage trước khi gửi lại.")
            with self.lock:
                if result["state"] in {"failed", "uncertain", "cancelled"} and result["comment_message"]:
                    result["comment_state"] = "cancelled"
                self.job["completed"] += 1
                self.save_job()
            if index < len(tasks) - 1:
                self.stop.wait(0.2 if demo else delay)
        with self.lock:
            self.job["state"] = "stopped" if self.stop.is_set() or self.shutdown.is_set() else "completed"
            self.job["finished_at"] = now()
            self.save_job()

    def close(self):
        self.shutdown.set()
        self.stop.set()
        if self.analytics is not None:
            self.analytics.close()
        if self.monitor is not None:
            self.monitor.stop()
        if self.worker:
            self.worker.join(timeout=2)
        # An in-flight video upload may still be reading a file on Windows.
        if not self.worker or not self.worker.is_alive():
            self.temp.cleanup()


class Handler(BaseHTTPRequestHandler):
    server_version = "PagePilot"

    def log_message(self, fmt, *args):
        pass  # Never log request bodies, paths containing credentials, or token values.

    @property
    def state(self):
        return self.server.state

    def valid_request(self, mutation=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self.respond(403, {"error": "Host không được phép."})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Yêu cầu từ website khác bị từ chối."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + expected:
            self.respond(403, {"error": "Origin không được phép."})
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.state.csrf):
            self.respond(403, {"error": "Phiên không hợp lệ. Tải lại giao diện."})
            return False
        return True

    def respond(self, code, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.valid_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/bootstrap":
            self.respond(200, self.state.bootstrap())
        elif path == "/api/jobs/current":
            with self.state.lock:
                self.respond(200, {"job": copy.deepcopy(self.state.job)})
        elif path.startswith("/api/jobs/"):
            job = self.state.get_job(path[len("/api/jobs/"):])
            self.respond(200, {"job": job}) if job else self.respond(404, {"error": "Không tìm thấy lượt đăng."})
        elif path == "/api/history":
            self.respond(200, {"history": self.state.history()})
        elif path == "/api/plans":
            self.respond(200, {"jobs": self.state.plans()})
        elif path == "/api/analytics":
            self.respond(200, self.state.get_analytics().snapshot())
        elif path in {"/", "/app.css", "/app.js", "/analytics.js"}:
            filename, mime = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/analytics.js": ("analytics.js", "text/javascript; charset=utf-8")}[path]
            self.respond(200, (ROOT / "static" / filename).read_bytes(), mime)
        elif path == "/favicon.ico":
            self.respond(204, b"", "image/x-icon")
        else:
            self.respond(404, {"error": "Không tìm thấy."})

    def body_size(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Yêu cầu phải có Content-Length.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Dung lượng yêu cầu không hợp lệ.")
        if not 0 < length <= limit:
            raise ValueError("Tệp hoặc yêu cầu vượt giới hạn dung lượng.")
        return length

    def do_POST(self):
        if not self.valid_request(mutation=True):
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/media":
                self.respond(200, self.upload())
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Yêu cầu phải dùng JSON.")
            data = json.loads(self.rfile.read(self.body_size(10 * 1024 * 1024)))
            if not isinstance(data, dict):
                raise ValueError("Dữ liệu không hợp lệ.")
            if path == "/api/connect":
                result = self.state.connect(data.get("access_token"), data.get("graph_version"))
            elif path == "/api/demo":
                result = self.state.connect_demo()
            elif path == "/api/disconnect":
                result = self.state.disconnect()
            elif path == "/api/jobs":
                result = self.state.begin_job(data)
            elif path == "/api/jobs/stop":
                self.state.stop.set()
                result = {"stopping": True}
            elif path == "/api/comments/cancel":
                result = self.state.cancel_comment(data.get("job_id"), data.get("result_index"))
            elif path == "/api/analytics/refresh":
                result = self.state.get_analytics().refresh(data.get("preset", "30days"))
            elif path == "/api/analytics/stop":
                result = self.state.get_analytics().stop()
            else:
                self.respond(404, {"error": "Không tìm thấy."})
                return
            self.respond(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except GraphError as exc:
            self.respond(502, {"error": str(exc)})
        except Exception:
            self.respond(500, {"error": "Có lỗi nội bộ. Không tự động gửi lại thao tác; kiểm tra trạng thái trước."})

    def upload(self):
        name = Path(unquote(self.headers.get("X-Filename", "")).replace("\\", "/")).name
        if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise ValueError("Tên tệp không hợp lệ.")
        suffix = Path(name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".mp4"}:
            raise ValueError("Chỉ hỗ trợ JPG, PNG và video MP4.")
        size = self.body_size(MAX_VIDEO if suffix == ".mp4" else MAX_PHOTO)
        with self.state.lock:
            if self.state.active():
                raise ValueError("Chờ hàng đợi hoàn tất trước khi đổi tệp.")
            if len(self.state.media) >= 1000:
                raise ValueError("Tối đa 1.000 tệp trong phiên. Xóa tệp không dùng trước.")
        if shutil.disk_usage(self.state.temp.name).free < size + 64 * 1024 * 1024:
            raise ValueError("Thư mục tệp tạm không đủ dung lượng. Giải phóng dung lượng hoặc chọn tệp nhỏ hơn.")
        media_id = secrets.token_hex(16)
        path = Path(self.state.temp.name) / (media_id + suffix)
        try:
            remaining = size
            first = b""
            with path.open("wb") as file:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Tệp tải lên chưa hoàn tất.")
                    if not first:
                        first = chunk[:64]
                    file.write(chunk)
                    remaining -= len(chunk)
            if suffix in {".jpg", ".jpeg"} and first.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif suffix == ".png" and first.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type = "image/png"
            elif suffix == ".mp4" and len(first) >= 12 and first[4:8] == b"ftyp":
                content_type = "video/mp4"
            else:
                raise ValueError("Nội dung tệp không khớp định dạng JPG/PNG/MP4.")
            media = {"id": media_id, "name": name, "content_type": content_type, "size": size, "path": str(path)}
            with self.state.lock:
                self.state.media[media_id] = media
            return {k: media[k] for k in ("id", "name", "content_type", "size")}
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def do_DELETE(self):
        if not self.valid_request(mutation=True):
            return
        prefix = "/api/media/"
        path = urlsplit(self.path).path
        if not path.startswith(prefix):
            self.respond(404, {"error": "Không tìm thấy."})
            return
        with self.state.lock:
            if self.state.active():
                self.respond(409, {"error": "Chờ hàng đợi hoàn tất trước khi xóa tệp."})
                return
            media = self.state.media.pop(path[len(prefix):], None)
            if media:
                Path(media["path"]).unlink(missing_ok=True)
            self.respond(200, {"deleted": bool(media)})


def make_server(port, state):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.state = state
    return server


def main():
    parser = argparse.ArgumentParser(description="PagePilot - đăng bài lên các Fanpage được cấp quyền.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args()
    try:
        server = make_server(args.port, None)
    except OSError:
        parser.exit(1, "Không mở được cổng. Thử chạy với --port 8766.\n")
    try:
        instance_lock = lock_data_dir(args.data_dir)
    except OSError:
        server.server_close()
        parser.exit(1, "PagePilot đang chạy với thư mục dữ liệu này. Dùng cửa sổ đang mở.\n")
    try:
        state = State(args.data_dir)
    except Exception:
        server.server_close()
        instance_lock.close()
        parser.exit(1, "Không mở được dữ liệu ứng dụng. Kiểm tra quyền truy cập thư mục dữ liệu.\n")
    server.state = state
    state.start_monitor()
    address = f"http://127.0.0.1:{server.server_port}"
    print("PagePilot đang chạy trên máy này. Nhấn Ctrl+C để thoát.")
    print("Địa chỉ cục bộ:", address)
    print("Lịch sử được lưu tại:", state.db_path)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nĐang dừng. Nếu có bài đang gửi, kiểm tra Fanpage trước khi gửi lại.")
    finally:
        server.server_close()
        state.close()
        instance_lock.close()


if __name__ == "__main__":
    main()
