"""Small, streaming Facebook Graph API client for pages the user manages.

Publishing requests are never retried. An acknowledged video upload is still
reported as processing until Meta explicitly confirms the publication.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests_toolbelt.multipart.encoder import MultipartEncoder


class GraphError(Exception):
    """Safe error text suitable for UI logs; never carries a server token."""

    def __init__(self, message: str, code: int | None = None, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain
        self.ambiguous = uncertain


class GraphClient:
    GRAPH_HOST = "graph.facebook.com"
    VIDEO_HOST = "graph-video.facebook.com"
    UPLOAD_HOST = "rupload.facebook.com"
    VERSION_RE = re.compile(r"v[1-9]\d?\.0\Z")
    OBJECT_ID_RE = re.compile(r"\d+(?:_\d+)?\Z")
    MIME_RE = re.compile(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+\Z")

    def __init__(
        self,
        user_token: str,
        graph_version: str,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10, 120),
        processing_timeout: float = 60,
        poll_interval: float = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(user_token, str) or not user_token.strip():
            raise GraphError("Cần User Access Token để lấy danh sách Fanpage.")
        if "\r" in user_token or "\n" in user_token:
            raise GraphError("Access Token không hợp lệ.")
        if not isinstance(graph_version, str) or not self.VERSION_RE.fullmatch(graph_version):
            raise GraphError("Phiên bản Graph API phải có dạng vNN.0, theo cấu hình Meta App của bạn.")
        self.user_token = user_token.strip()
        self.graph_version = graph_version
        self.base_url = f"https://{self.GRAPH_HOST}/{graph_version}"
        self.session = session if session is not None else requests.Session()
        if session is None:
            # requests defaults to no retries; make that publishing invariant explicit.
            self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.timeout = timeout
        self.processing_timeout = max(0, processing_timeout)
        self.poll_interval = max(0, poll_interval)
        self._sleep = sleep
        self._clock = clock
        self._known_tokens = {self.user_token}

    def close(self) -> None:
        self.session.close()

    def list_pages(self) -> list[dict[str, Any]]:
        """Read every /me/accounts page, without following arbitrary next URLs."""
        pages: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        seen_cursors: set[str] = set()
        params: dict[str, Any] = {"fields": "id,name,access_token,tasks", "limit": 100}
        while True:
            payload = self._request("GET", "me/accounts", self.user_token, params=params)
            data = payload.get("data")
            if not isinstance(data, list):
                raise GraphError("Meta không trả về danh sách Fanpage hợp lệ.")
            for item in data:
                if not isinstance(item, dict):
                    raise GraphError("Danh sách Fanpage từ Meta có dữ liệu không hợp lệ.")
                page_id = self._id(item.get("id"))
                page_token = item.get("access_token", "")
                if not isinstance(page_token, str) or "\r" in page_token or "\n" in page_token:
                    raise GraphError("Meta trả về Page Access Token không hợp lệ.")
                if page_token:
                    self._known_tokens.add(page_token)
                if page_id not in seen_pages:
                    tasks = item.get("tasks", [])
                    pages.append({
                        "id": page_id,
                        "name": item.get("name") if isinstance(item.get("name"), str) else page_id,
                        "access_token": page_token,
                        "tasks": [task for task in tasks if isinstance(task, str)] if isinstance(tasks, list) else [],
                    })
                    seen_pages.add(page_id)
            paging = payload.get("paging", {})
            next_url = paging.get("next") if isinstance(paging, dict) else None
            if not next_url:
                return pages
            next_parts = self._trusted_url(next_url, self.GRAPH_HOST)
            # Meta may canonicalize /me to the numeric user ID in paging.next.
            # Only this accounts edge is accepted; the actual next request is
            # still rebuilt as /me/accounts with the original user token.
            accounts_path = rf"/{re.escape(self.graph_version)}/(?:me|\d+)/accounts\Z"
            if not re.fullmatch(accounts_path, next_parts.path):
                raise GraphError("Đường dẫn phân trang Meta không hợp lệ; đã dừng để bảo vệ token.")
            query = parse_qs(next_parts.query)
            cursors = query.get("after", [])
            if len(cursors) != 1 or not cursors[0] or cursors[0] in seen_cursors:
                raise GraphError("Con trỏ phân trang Meta không hợp lệ; hãy tải lại danh sách Fanpage.")
            cursor = cursors[0]
            seen_cursors.add(cursor)
            # Reconstruct the trusted endpoint and copy only the cursor. In particular,
            # a token embedded in Meta's next URL never becomes a query parameter.
            params = {"fields": "id,name,access_token,tasks", "limit": 100, "after": cursor}

    def publish(
        self,
        page: dict[str, Any],
        kind: str,
        message: str,
        media: list[dict[str, Any]],
        title: str = "",
    ) -> dict[str, Any]:
        page_id = self._id(page.get("id"))
        page_token = page.get("access_token")
        if not isinstance(page_token, str) or not page_token.strip() or "\r" in page_token or "\n" in page_token:
            raise GraphError("Fanpage thiếu Page Access Token hợp lệ; hãy kết nối lại tài khoản.")
        self._known_tokens.add(page_token)
        if not isinstance(message, str) or not isinstance(title, str) or not isinstance(media, list):
            raise GraphError("Nội dung bài đăng không hợp lệ.")
        if kind == "text":
            if not message.strip() or media:
                raise GraphError("Bài văn bản cần nội dung và không kèm tệp phương tiện.")
            payload = self._request("POST", f"{page_id}/feed", page_token, data={"message": message}, may_publish=True)
            return self._result(self._published_id(payload), "published", payload)
        if kind == "photos":
            if not 1 <= len(media) <= 10:
                raise GraphError("Bài ảnh cần từ 1 đến 10 ảnh.")
            files = [self._media_file(item, "image/") for item in media]
            attached_media = []
            for file_info in files:
                payload = self._multipart(
                    f"{page_id}/photos", page_token, file_info,
                    {"published": "false"}, may_publish=False,
                )
                attached_media.append({"media_fbid": self._id(payload.get("id"))})
            payload = self._request(
                "POST", f"{page_id}/feed", page_token,
                data={"message": message, "attached_media": json.dumps(attached_media, separators=(",", ":"))},
                may_publish=True,
            )
            return self._result(self._published_id(payload), "published", payload)
        if kind not in ("video", "reel"):
            raise GraphError("Loại bài đăng phải là text, photos, video hoặc reel.")
        if len(media) != 1:
            raise GraphError("Bài video/Reel cần đúng một tệp video.")
        file_info = self._media_file(media[0], "video/")
        if kind == "video":
            fields = {"description": message, "title": title, "published": "true"}
            payload = self._multipart(
                f"{page_id}/videos", page_token, file_info, fields, may_publish=True,
                url=f"https://{self.VIDEO_HOST}/{self.graph_version}/{page_id}/videos",
            )
            video_id = self._published_id(payload)
            return self._poll_published(video_id, page_token, kind, self._clock() + self.processing_timeout)
        return self._publish_reel(page_id, page_token, message, title, file_info)

    def _publish_reel(self, page_id: str, token: str, message: str, title: str, file_info: tuple[Path, str, str]) -> dict[str, Any]:
        payload = self._request("POST", f"{page_id}/video_reels", token, data={"upload_phase": "start"})
        video_id = self._id(payload.get("video_id"))
        # Older official samples construct this URL from the returned video ID.
        upload_url = payload.get("upload_url", f"https://{self.UPLOAD_HOST}/video-upload/{self.graph_version}/{video_id}")
        upload_parts = self._trusted_url(upload_url, self.UPLOAD_HOST)
        if upload_parts.query or upload_parts.path != f"/video-upload/{self.graph_version}/{video_id}":
            raise GraphError("Địa chỉ tải Reel chứa tham số ngoài giao thức dự kiến; đã dừng để bảo vệ token.")
        path, _, _ = file_info
        with path.open("rb") as source:
            uploaded = self._request(
                "POST", "", token, url=upload_url, data=source,
                headers={"Authorization": f"OAuth {token}", "Content-Type": "application/octet-stream", "offset": "0", "file_size": str(path.stat().st_size)},
            )
        if uploaded.get("success") is not True:
            raise GraphError("Meta chưa xác nhận nhận đủ tệp Reel; chưa gửi lệnh đăng.")
        deadline = self._clock() + self.processing_timeout
        self._wait_upload(video_id, token, min(deadline, self._clock() + 15))
        finished = self._request(
            "POST", f"{page_id}/video_reels", token,
            data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": message, "title": title},
            may_publish=True,
        )
        if finished.get("success") is not True:
            raise GraphError("Meta chưa xác nhận lệnh đăng Reel. Kiểm tra Fanpage trước khi thử lại.", uncertain=True)
        return self._poll_published(video_id, token, "reel", deadline)

    def _wait_upload(self, video_id: str, token: str, deadline: float) -> None:
        # Successful raw upload is Meta's acknowledgement of the bytes. A status
        # check additionally catches an explicitly unfinished or failed upload.
        for _ in range(21):
            payload = self._video_status(video_id, token, "reel", deadline)
            status = self._status(payload)
            self._raise_video_failure(status)
            uploading = status.get("uploading_phase", {})
            phase = uploading.get("status") if isinstance(uploading, dict) else None
            if phase not in ("in_progress", "uploading", "processing", "not_started"):
                return
            if self._clock() >= deadline:
                raise GraphError("Meta vẫn đang nhận tệp Reel; chưa gửi lệnh đăng. Hãy kiểm tra video trong Meta trước khi thử lại.")
            self._sleep(min(self.poll_interval, max(0, deadline - self._clock())))
        raise GraphError("Chưa xác nhận hoàn tất tải Reel; chưa gửi lệnh đăng.")

    def _poll_published(self, video_id: str, token: str, kind: str, deadline: float) -> dict[str, Any]:
        for _ in range(21):
            try:
                payload = self._video_status(video_id, token, kind, deadline)
            except GraphError:
                # The publish command already returned an object ID. Do not turn
                # a status-read failure into a retry of the publishing command.
                return self._result(video_id, "processing")
            status = self._status(payload)
            self._raise_video_failure(status)
            publishing = status.get("publishing_phase", {})
            complete = isinstance(publishing, dict) and publishing.get("status") == "complete"
            if complete or (kind == "video" and payload.get("published") is True):
                return self._result(video_id, "published", payload)
            if self._clock() >= deadline:
                return self._result(video_id, "processing", payload)
            self._sleep(min(self.poll_interval, max(0, deadline - self._clock())))
        return self._result(video_id, "processing")

    def _video_status(self, video_id: str, token: str, kind: str, deadline: float) -> dict[str, Any]:
        fields = "status" if kind == "reel" else "status,published,permalink_url"
        remaining = max(1, deadline - self._clock())
        return self._request("GET", video_id, token, params={"fields": fields}, timeout=(min(5, remaining), min(10, remaining)))

    @staticmethod
    def _status(payload: dict[str, Any]) -> dict[str, Any]:
        status = payload.get("status", {})
        return status if isinstance(status, dict) else {}

    @staticmethod
    def _raise_video_failure(status: dict[str, Any]) -> None:
        failed = status.get("video_status") == "error"
        for name in ("uploading_phase", "processing_phase", "publishing_phase"):
            phase = status.get(name, {})
            if isinstance(phase, dict) and (phase.get("status") in ("error", "failed") or phase.get("errors")):
                failed = True
        if failed:
            raise GraphError("Meta báo lỗi xử lý hoặc xuất bản video. Kiểm tra định dạng video và trạng thái bài trong Meta.")

    def _multipart(self, path: str, token: str, file_info: tuple[Path, str, str], fields: dict[str, str], *, may_publish: bool, url: str | None = None) -> dict[str, Any]:
        file_path, name, content_type = file_info
        with ExitStack() as stack:
            source = stack.enter_context(file_path.open("rb"))
            encoder = MultipartEncoder(fields={**fields, "source": (name, source, content_type)})
            return self._request("POST", path, token, data=encoder, headers={"Content-Type": encoder.content_type}, may_publish=may_publish, url=url)

    def _request(
        self, method: str, path: str, token: str, *, params: dict[str, Any] | None = None,
        data: Any = None, headers: dict[str, str] | None = None, may_publish: bool = False,
        url: str | None = None, timeout: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        request_headers.update(headers or {})
        try:
            response = self.session.request(
                method, url or f"{self.base_url}/{path}", params=params, data=data, headers=request_headers,
                timeout=timeout or self.timeout, allow_redirects=False, verify=True,
            )
        except requests.RequestException:
            message = "Không thể kết nối tới Meta. Kiểm tra mạng và cấu hình API."
            if may_publish:
                message += " Meta có thể đã nhận lệnh đăng; kiểm tra Fanpage trước khi thử lại."
            raise GraphError(message, uncertain=may_publish) from None
        if 300 <= response.status_code < 400:
            raise GraphError("Meta trả về chuyển hướng ngoài giao thức dự kiến; đã dừng để bảo vệ token.", uncertain=may_publish)
        try:
            payload = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise GraphError("Meta trả về phản hồi không hợp lệ; kiểm tra trạng thái bài trước khi thử lại.", uncertain=may_publish) from None
        if not isinstance(payload, dict):
            raise GraphError("Meta trả về dữ liệu không hợp lệ.", uncertain=may_publish)
        error = payload.get("error")
        if isinstance(error, dict) or not 200 <= response.status_code < 300:
            raw_code = error.get("code") if isinstance(error, dict) else None
            code = raw_code if isinstance(raw_code, int) else None
            reasons = {
                190: "Access Token hết hạn hoặc không hợp lệ; hãy kết nối lại tài khoản.",
                10: "Meta App hoặc tài khoản thiếu quyền cho thao tác này.",
                200: "Tài khoản thiếu quyền quản lý nội dung Fanpage.",
                4: "Meta đang giới hạn số yêu cầu; chờ rồi kiểm tra trạng thái bài.",
                17: "Meta đang giới hạn số yêu cầu; chờ rồi kiểm tra trạng thái bài.",
                32: "Meta đang giới hạn số yêu cầu Fanpage; chờ rồi kiểm tra trạng thái bài.",
                613: "Meta đang giới hạn số yêu cầu; chờ rồi kiểm tra trạng thái bài.",
                100: "Thông số hoặc tệp đăng bài không được phiên bản API này chấp nhận.",
                368: "Meta đã hạn chế thao tác này; kiểm tra trạng thái tài khoản/Fanpage.",
            }
            reason = reasons.get(code, "Meta chưa chấp nhận thao tác; kiểm tra quyền và trạng thái Fanpage.")
            message = f"Meta báo lỗi (mã {code}). {reason}" if code is not None else reason
            uncertain = may_publish and (response.status_code >= 500 or code in (1, 2))
            if uncertain:
                message += " Meta có thể đã nhận lệnh đăng; kiểm tra Fanpage trước khi thử lại."
            # Never pass through Meta's message, request URLs, or exception text:
            # all can contain a token, especially when a proxy is involved.
            raise GraphError(message, code=code, uncertain=uncertain)
        return payload

    def _result(self, object_id: str, status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"id": object_id, "status": status}
        permalink = (payload or {}).get("permalink_url")
        if isinstance(permalink, str) and not any(token in permalink for token in self._known_tokens):
            try:
                parts = urlsplit(permalink)
                if parts.scheme == "https" and parts.netloc in ("facebook.com", "www.facebook.com", "m.facebook.com") and not parts.query and not parts.fragment:
                    result["permalink_url"] = permalink
            except ValueError:
                pass
        if "permalink_url" not in result and status == "published":
            result["permalink_url"] = f"https://www.facebook.com/{object_id}"
        return result

    @classmethod
    def _trusted_url(cls, url: Any, host: str):
        if not isinstance(url, str):
            raise GraphError("Địa chỉ tải hoặc phân trang Meta không hợp lệ.")
        try:
            parts = urlsplit(url)
            valid = parts.scheme == "https" and parts.netloc == host and not parts.fragment
        except ValueError:
            valid = False
        if not valid:
            raise GraphError("Địa chỉ tải hoặc phân trang không thuộc máy chủ Meta cho phép; đã dừng để bảo vệ token.")
        return parts

    @classmethod
    def _id(cls, value: Any) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not cls.OBJECT_ID_RE.fullmatch(value):
            raise GraphError("Meta ID không hợp lệ.")
        return value

    @classmethod
    def _published_id(cls, payload: dict[str, Any]) -> str:
        try:
            return cls._id(payload.get("id"))
        except GraphError:
            raise GraphError("Meta chưa trả về mã bài đăng hợp lệ. Meta có thể đã nhận lệnh đăng; kiểm tra Fanpage trước khi thử lại.", uncertain=True) from None

    @classmethod
    def _media_file(cls, item: Any, prefix: str) -> tuple[Path, str, str]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise GraphError("Tệp phương tiện không hợp lệ.")
        path = Path(item["path"])
        if not path.is_absolute() or not path.is_file() or path.stat().st_size == 0:
            raise GraphError("Không tìm thấy tệp phương tiện có nội dung; hãy chọn lại tệp.")
        name = item.get("name", path.name)
        mime = item.get("content_type", "")
        if not isinstance(name, str) or not name or "\r" in name or "\n" in name:
            raise GraphError("Tên tệp phương tiện không hợp lệ.")
        if not isinstance(mime, str) or not cls.MIME_RE.fullmatch(mime) or not mime.startswith(prefix):
            raise GraphError("Loại tệp phương tiện không phù hợp với loại bài đăng.")
        return path, name, mime
