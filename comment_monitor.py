"""Observe Meta publication and send one durable, Page-authored comment.

Scheduling itself belongs to Meta. This local observer never publishes a post,
and it never treats a scheduled time as proof that a real post is public.
Only access tokens already held by State in memory are used here.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import nullcontext
from typing import Any, Callable

from meta_client import GraphError


class CommentMonitor:
    PENDING_COMMENTS = {"", "waiting_post", "waiting_delay", "waiting_connection"}
    POST_STATES = {"scheduled", "processing", "published"}

    def __init__(self, state: Any, interval: float = 5, clock: Callable[[], float] = time.time):
        self.state = state
        self.interval = max(0.05, float(interval))
        self.clock = clock
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._writing = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        with self._thread_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="pagepilot-comment-monitor", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            # A request may still be completing. Never freeze application shutdown
            # indefinitely while waiting for a network timeout.
            thread.join(timeout=2)

    def busy(self) -> bool:
        """Connection changes must wait while this instance uses a client."""
        return self._busy.is_set()

    @property
    def active_write(self) -> bool:
        return self._writing.is_set()

    def _halted(self) -> bool:
        return self._stop.is_set() or self.state.shutdown.is_set()

    def _run(self) -> None:
        while not self._halted():
            try:
                self.tick()
            except Exception:
                # Persistence or shutdown can interrupt a scan. A claimed write
                # remains "sending" on disk and is never retried by this monitor.
                pass
            self._stop.wait(self.interval)

    def tick(self) -> None:
        """Run one scan; safe to call directly in tests without starting a thread."""
        if self._halted() or not self._tick_lock.acquire(blocking=False):
            return
        try:
            for job in self.state.plans():
                if self._halted():
                    return
                for index, result in enumerate(job.get("results", [])):
                    if self._halted():
                        return
                    # Older histories had no comment lifecycle. Preserve them.
                    if "comment_state" not in result or result.get("state") not in self.POST_STATES:
                        continue
                    self._observe(job, index, result)
        finally:
            self._tick_lock.release()

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        return None

    @staticmethod
    def _expected(result: dict[str, Any]) -> dict[str, Any]:
        return {key: result.get(key) for key in ("post_id", "state", "comment_state")}

    @staticmethod
    def _has_pending_comment(result: dict[str, Any]) -> bool:
        message = result.get("comment_message")
        return isinstance(message, str) and bool(message.strip()) and result.get("comment_state") in CommentMonitor.PENDING_COMMENTS

    def _update(self, job_id: str, index: int, result: dict[str, Any], patch: dict[str, Any]) -> bool:
        return self.state.update_result(job_id, index, patch, expected=self._expected(result))

    def _current(self, job_id: str, index: int, expected: dict[str, Any]) -> dict[str, Any] | None:
        for job in self.state.plans():
            if job.get("id") == job_id:
                results = job.get("results", [])
                if not 0 <= index < len(results):
                    return None
                current = results[index]
                if all(current.get(key) == value for key, value in expected.items()):
                    return current
                return None
        return None

    def _connection(self, job: dict[str, Any], result: dict[str, Any]) -> tuple[Any, dict[str, Any] | None, bool]:
        """Read under State.lock; a demo job can never use a real client."""
        demo = bool(job.get("demo"))
        if self.state.connecting or demo != bool(self.state.demo):
            return None, None, False
        page = next((page for page in self.state.pages if str(page.get("id")) == str(result.get("page_id"))), None)
        if page is None or (not demo and self.state.client is None):
            return None, None, False
        return self.state.client, page, True

    def _waiting_connection(self, job: dict[str, Any], index: int, result: dict[str, Any]) -> None:
        if self._has_pending_comment(result) and result.get("comment_state") != "waiting_connection":
            self._update(job["id"], index, result, {"comment_state": "waiting_connection"})

    def _observe(self, job: dict[str, Any], index: int, result: dict[str, Any]) -> None:
        with self.state.lock:
            _, _, available = self._connection(job, result)
            if not available:
                self._waiting_connection(job, index, result)
                return

        if result["state"] == "scheduled":
            scheduled_at = self._timestamp(result.get("scheduled_at"))
            if scheduled_at is not None and self.clock() < scheduled_at:
                if self._has_pending_comment(result) and result["comment_state"] == "waiting_connection":
                    self._update(job["id"], index, result, {"comment_state": "waiting_post"})
                return

        if result["state"] in {"scheduled", "processing"}:
            result = self._check_publication(job, index, result)
            if result is None or result.get("state") != "published":
                return

        if not self._has_pending_comment(result):
            return
        published_at = self._timestamp(result.get("published_at"))
        if published_at is None:
            published_at = self.clock()
        due_at = self._timestamp(result.get("comment_due_at"))
        if due_at is None:
            delay = self._timestamp(result.get("comment_delay_seconds")) or 0
            due_at = published_at + max(0, delay)
        patch = {"published_at": published_at, "comment_due_at": due_at, "comment_state": "waiting_delay"}
        if any(result.get(key) != value for key, value in patch.items()) and not self._update(job["id"], index, result, patch):
            return
        result = {**result, **patch}
        if self.clock() >= due_at:
            self._send_comment(job, index, result)

    def _api_context(self):
        return getattr(self.state, "api_lock", None) or nullcontext()

    def _check_publication(self, job: dict[str, Any], index: int, result: dict[str, Any]) -> dict[str, Any] | None:
        if not result.get("post_id"):
            return None
        with self._api_context():
            with self.state.lock:
                current = self._current(job["id"], index, self._expected(result))
                if current is None or self._halted():
                    return None
                client, page, available = self._connection(job, current)
                if not available:
                    self._waiting_connection(job, index, current)
                    return None
                self._busy.set()
            try:
                if job.get("demo"):
                    checked = {"status": "published"}
                else:
                    checked = client.check_publication(page, current["post_id"], current.get("kind", "text"))
            except GraphError as exc:
                checked = {"status": "processing", "error": self._safe_error(exc, client, page)}
            except Exception:
                checked = {"status": "processing", "error": "Chưa kiểm tra được trạng thái bài đăng; sẽ kiểm tra lại."}
            finally:
                with self.state.lock:
                    self._busy.clear()
        status = checked.get("status") if isinstance(checked, dict) else None
        if status == "published":
            published_at = self.clock()
            patch: dict[str, Any] = {"state": "published", "published_at": published_at, "publication_error": ""}
            if self._has_pending_comment(current):
                delay = self._timestamp(current.get("comment_delay_seconds")) or 0
                patch.update(comment_state="waiting_delay", comment_due_at=published_at + max(0, delay))
        elif status == "failed":
            reason = self._safe_error(checked.get("error") or "Meta báo bài đăng không thể xuất bản.", client, page)
            patch = {"state": "failed", "error": reason, "publication_error": reason}
            if self._has_pending_comment(current):
                patch.update(comment_state="cancelled", comment_error="Không bình luận vì bài đăng xuất bản thất bại.")
        else:
            # A failed GET provides no evidence about publication. Keep the
            # scheduled/processing state and never retry a publishing request.
            patch = {"publication_error": self._safe_error(checked.get("error", ""), client, page) if isinstance(checked, dict) else "Meta chưa trả về trạng thái bài đăng hợp lệ."}
            if self._has_pending_comment(current):
                patch["comment_state"] = "waiting_post"
        if any(current.get(key) != value for key, value in patch.items()) and not self._update(job["id"], index, current, patch):
            return None
        return {**current, **patch}

    def _send_comment(self, job: dict[str, Any], index: int, result: dict[str, Any]) -> None:
        if not result.get("post_id"):
            return
        with self._api_context():
            with self.state.lock:
                current = self._current(job["id"], index, self._expected(result))
                if current is None or self._halted():
                    return
                client, page, available = self._connection(job, current)
                if not available:
                    self._waiting_connection(job, index, current)
                    return
                if not self._update(job["id"], index, current, {"comment_state": "sending", "comment_error": ""}):
                    return
                self._busy.set()
                self._writing.set()
            try:
                if job.get("demo"):
                    comment = {"id": f"demo-comment-{job['id']}-{index}", "status": "published"}
                else:
                    comment = client.comment(page, current["post_id"], current["comment_message"])
                if not isinstance(comment, dict) or not comment.get("id") or comment.get("status") != "published":
                    raise GraphError("Meta chưa xác nhận mã bình luận. Kiểm tra bài đăng trước khi thử lại.", uncertain=True)
                patch = {"comment_state": "commented", "comment_id": comment["id"], "comment_error": ""}
            except GraphError as exc:
                patch = {"comment_state": "uncertain" if exc.uncertain else "failed", "comment_error": self._safe_error(exc, client, page)}
            except Exception:
                patch = {"comment_state": "uncertain", "comment_error": "Chưa xác định được kết quả bình luận. Kiểm tra bài đăng trước khi thử lại."}
            finally:
                with self.state.lock:
                    self._writing.clear()
                    self._busy.clear()
        self.state.update_result(
            job["id"], index, patch,
            expected={"post_id": current["post_id"], "state": "published", "comment_state": "sending"},
        )

    def _safe_error(self, error: Any, client: Any, page: dict[str, Any] | None) -> str:
        text = str(error)
        # GraphClient already redacts errors; additionally protect injected
        # clients so errors never turn in-memory credentials into history.
        tokens = [getattr(client, "user_token", None)]
        if page is not None:
            tokens.append(page.get("access_token"))
        with self.state.lock:
            tokens.extend(page.get("access_token") for page in self.state.pages)
        for token in tokens:
            if isinstance(token, str) and token:
                text = text.replace(token, "[token đã ẩn]")
        return text[:1200]
