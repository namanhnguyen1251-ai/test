"""PagePilot: a local, explicit-review publishing queue for managed Facebook Pages."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
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
FINISHED = {"published", "processing", "failed", "uncertain", "cancelled"}


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
                if job.get("state") == "running":
                    job["state"] = "stopped"
                    job["finished_at"] = now()
                    for result in job.get("results", []):
                        if result["state"] == "running":
                            result.update(state="uncertain", error="Phiên trước bị đóng khi đang gửi. Kiểm tra Fanpage trước khi gửi lại.")
                        elif result["state"] == "queued":
                            result["state"] = "cancelled"
                    job["completed"] = len(job.get("results", []))
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))

    def active(self):
        return self.job is not None and self.job["state"] == "running"

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

    def save_job(self):
        payload = json.dumps(self.job, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO jobs (id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (self.job["id"], payload))
            db.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY rowid DESC LIMIT 100)")

    def bootstrap(self):
        with self.lock:
            return {"csrf_token": self.csrf, "connected": bool(self.pages), "demo": self.demo,
                    "graph_version": self.graph_version, "pages": self.public_pages(),
                    "job": copy.deepcopy(self.job), "media": self.public_media(), "history": self.history()}

    def connect(self, token, version):
        if not isinstance(token, str) or not 10 <= len(token) <= 8192 or re.search(r"\s", token):
            raise ValueError("Access token không hợp lệ. Dán token của user đã có quyền quản lý Fanpage.")
        if not isinstance(version, str) or not re.fullmatch(r"v[1-9]\d?\.0", version):
            raise ValueError("Nhập phiên bản Graph API đang dùng trong Meta App, ví dụ theo dạng vXX.0.")
        with self.lock:
            if self.active() or self.connecting:
                raise ValueError("Chờ hàng đợi hoặc thao tác kết nối hiện tại hoàn tất.")
            self.connecting = True
        try:
            client = self.client_factory(token, version)
            pages = client.list_pages()
            with self.lock:
                self.client, self.pages = client, pages
                self.demo, self.graph_version = False, version
            return {"pages": self.public_pages(), "demo": False}
        finally:
            with self.lock:
                self.connecting = False

    def connect_demo(self):
        with self.lock:
            if self.active() or self.connecting:
                raise ValueError("Chờ hàng đợi hoặc thao tác kết nối hiện tại hoàn tất.")
            self.client = None
            self.demo = True
            self.graph_version = "demo"
            self.pages = [{"id": str(100000 + i), "name": f"Fanpage thử nghiệm {i:02d}",
                           "access_token": "demo-only", "tasks": ["CREATE_CONTENT"]} for i in range(1, 51)]
            return {"pages": self.public_pages(), "demo": True}

    def disconnect(self):
        with self.lock:
            if self.active() or self.connecting:
                raise ValueError("Dừng hàng đợi và chờ bài đang gửi hoàn tất trước khi ngắt kết nối.")
            self.client = None
            self.pages = []
            self.demo = False
            self.graph_version = ""
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
            if self.active() or self.connecting:
                raise ValueError("Đã có hàng đợi hoặc thao tác kết nối đang chạy.")
            if not self.pages:
                raise ValueError("Kết nối Meta App hoặc bật chế độ dùng thử trước.")
            ids = data.get("page_ids")
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
            kind = data.get("kind")
            if kind not in {"text", "photos", "video", "reel"}:
                raise ValueError("Loại nội dung không hợp lệ.")
            message = data.get("message", "")
            title = data.get("title", "")
            if not isinstance(message, str) or len(message) > 63206 or not isinstance(title, str) or len(title) > 255:
                raise ValueError("Nội dung hoặc tiêu đề vượt giới hạn của tool.")
            if kind == "text" and not message.strip():
                raise ValueError("Nhập nội dung bài viết.")
            media_ids = data.get("media_ids", [])
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
            delay = data.get("delay_seconds", 3)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 1 <= delay <= 60:
                raise ValueError("Khoảng nghỉ giữa các trang phải từ 1 đến 60 giây.")
            self.stop.clear()
            self.job = {"id": request_id or secrets.token_hex(12), "state": "running", "kind": kind, "message": message,
                        "title": title, "created_at": now(), "demo": self.demo, "total": len(pages), "completed": 0,
                        "results": [{"page_id": str(p["id"]), "page_name": p["name"], "state": "queued"} for p in pages]}
            self.save_job()
            self.worker = threading.Thread(target=self.run_job, args=(pages, kind, message, title, media, delay, self.client, self.demo), daemon=True)
            self.worker.start()
            return {"job": copy.deepcopy(self.job)}

    def run_job(self, pages, kind, message, title, media, delay, client, demo):
        for index, page in enumerate(pages):
            with self.lock:
                result = self.job["results"][index]
                if self.stop.is_set() or self.shutdown.is_set():
                    result["state"] = "cancelled"
                    self.job["completed"] += 1
                    self.save_job()
                    continue
                result["state"] = "running"
                self.save_job()
            try:
                if demo:
                    time.sleep(0.15)
                    output = {"id": f"demo_{page['id']}", "status": "published"}
                else:
                    output = client.publish(page, kind, message, media, title=title)
                with self.lock:
                    result.update(state=output.get("status", "processing"), post_id=str(output.get("id", "")))
                    if result["state"] not in {"published", "processing"}:
                        result["state"] = "processing"
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
                self.job["completed"] += 1
                self.save_job()
            if index < len(pages) - 1:
                self.stop.wait(0.2 if demo else delay)
        with self.lock:
            self.job["state"] = "stopped" if self.stop.is_set() or self.shutdown.is_set() else "completed"
            self.job["finished_at"] = now()
            self.save_job()

    def close(self):
        self.shutdown.set()
        self.stop.set()
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
        elif path == "/api/history":
            self.respond(200, {"history": self.state.history()})
        elif path in {"/", "/app.css", "/app.js"}:
            filename, mime = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8")}[path]
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
            data = json.loads(self.rfile.read(self.body_size(256 * 1024)))
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
            if len(self.state.media) >= 20:
                raise ValueError("Tối đa 20 tệp trong phiên. Xóa tệp không dùng trước.")
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
