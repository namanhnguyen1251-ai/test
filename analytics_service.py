"""Read-only, cancellable analytics collection for the current Meta session.

Only public, token-free reports are persisted. A report is a snapshot: current
followers are not added over daily samples, and missing values remain unknown.
"""

from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from meta_client import GraphError
from page_analytics import AnalyticsCancelled, METRIC_SPECS, fetch_page_analytics


METRICS = copy.deepcopy(METRIC_SPECS)
PRESETS = {
    "today": (1, "today", "Hôm nay"),
    "7days": (7, "last_7d", "7 ngày gần nhất"),
    "30days": (30, "last_30d", "30 ngày gần nhất"),
}


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _SessionClient:
    """Check identity at each request, including pagination inside a fetcher."""

    def __init__(self, client: Any, check: Callable[[], None]):
        self._client = client
        self._check = check

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _request(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        if not args or args[0] != "GET":
            raise GraphError("Bảng thống kê chỉ được phép đọc dữ liệu.")
        result = self._client._request(*args, **kwargs)
        self._check()
        return result


class AnalyticsService:
    def __init__(self, state: Any, fetcher: Callable = fetch_page_analytics,
                 clock: Callable[[], float] = time.time):
        self.state = state
        self.fetcher = fetcher
        self.clock = clock
        self._generation = 0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._identity: tuple[Any, bool, tuple[str, ...], Any] | None = None
        self._report: dict[str, Any] | None = None
        with self.state.lock:
            self._load_cache()

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def _ids(self) -> tuple[str, ...]:
        return tuple(str(page.get("id", "")) for page in self.state.pages)

    def _identity_matches(self) -> bool:
        if self._identity is None:
            return not self.state.pages and self.state.client is None and not self.state.demo
        client, demo, page_ids, revision = self._identity
        return (self.state.client is client and bool(self.state.demo) == demo and self._ids() == page_ids
                and getattr(self.state, "session_revision", None) == revision)

    def _load_cache(self) -> None:
        with sqlite3.connect(self.state.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS analytics_reports (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            saved = db.execute("SELECT payload FROM analytics_reports WHERE id='latest'").fetchone()
        # A cache from a previous account is only an explicitly labelled offline
        # snapshot. Connecting any session clears it, even with the same Page IDs.
        if not saved or self.state.pages or self.state.client is not None or self.state.demo:
            return
        try:
            report = json.loads(saved[0])
            if (not isinstance(report, dict) or report.get("state") not in {"running", "completed", "cancelled"}
                    or report.get("preset") not in PRESETS or not isinstance(report.get("rows"), list)):
                return
            if any(not isinstance(row, dict) or not isinstance(row.get("metrics"), dict)
                   or any(not isinstance(row["metrics"].get(key), dict) for key in METRICS)
                   for row in report["rows"]):
                return
            if report.get("state") == "running":
                report["state"] = "cancelled"
                report["finished_at"] = self._now()
                report["interrupted"] = True
                self._remaining_not_loaded(report)
                report["summary"] = self._summary(report["rows"])
            report["cached"] = True
            self._report = report
        except (ValueError, TypeError):
            self._report = None

    def _save(self) -> None:
        if self._report is None:
            return
        with sqlite3.connect(self.state.db_path) as db:
            db.execute("INSERT INTO analytics_reports(id,payload) VALUES('latest',?) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (json.dumps(self._report, ensure_ascii=False),))

    def snapshot(self) -> dict[str, Any]:
        with self.state.lock:
            if not self._identity_matches():
                self.invalidate()
            return {"report": copy.deepcopy(self._report)}

    def refresh(self, preset: str = "30days") -> dict[str, Any]:
        if not isinstance(preset, str) or preset not in PRESETS:
            raise ValueError("Chọn Hôm nay, 7 ngày gần nhất hoặc 30 ngày gần nhất.")
        with self.state.lock:
            if self._closed or self.state.shutdown.is_set():
                raise ValueError("Ứng dụng đang đóng.")
            if self.state.connecting:
                raise ValueError("Chờ kết nối Fanpage hoàn tất trước khi tải thống kê.")
            if not self.state.pages or (not self.state.demo and self.state.client is None):
                raise ValueError("Kết nối tài khoản và tải danh sách Fanpage trước.")
            active = getattr(self.state, "active", None)
            if callable(active) and active():
                raise ValueError("Chờ lượt đăng hiện tại hoàn tất trước khi tải thống kê.")
            if self._report is not None and self._report.get("state") == "running" and self._identity_matches():
                raise ValueError("Đang tải thống kê. Chờ hoàn tất hoặc dừng lượt tải hiện tại.")
            self._cancel.set()
            self._generation += 1
            generation = self._generation
            cancel = self._cancel = threading.Event()
            client = self.state.client
            demo = bool(self.state.demo)
            pages = copy.deepcopy(self.state.pages)
            self._identity = (client, demo, self._ids(), getattr(self.state, "session_revision", None))
            days, native_preset, label = PRESETS[preset]
            current = datetime.fromtimestamp(self.clock(), timezone.utc)
            since = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
            date_range = {"preset": preset, "days": days, "date_preset": native_preset,
                          "label": label, "since": int(since.timestamp()),
                          "until": max(int(current.timestamp()), int(since.timestamp()) + 1)}
            rows = [self._empty_row(page, "queued", demo=demo) for page in pages]
            tokens = self._tokens(client, pages)
            for row in rows:
                row["page_name"] = self._text(row["page_name"], tokens)
            timestamp = self._now()
            self._report = {"id": secrets.token_urlsafe(12), "state": "running", "preset": preset,
                            "range": date_range, "started_at": timestamp, "updated_at": timestamp,
                            "finished_at": None, "total_pages": len(rows), "completed_pages": 0,
                            "rows": rows, "summary": self._summary(rows), "demo": demo, "cached": False}
            self._save()
            initial = {"report": copy.deepcopy(self._report)}
            thread = threading.Thread(target=self._collect, args=(generation, cancel, client, demo, pages, date_range),
                                      name="pagepilot-analytics", daemon=True)
            self._thread = thread
            self._threads.add(thread)
            thread.start()
            return initial

    @staticmethod
    def _empty_row(page: dict[str, Any], status: str, *, demo: bool = False) -> dict[str, Any]:
        row = {"page_id": str(page.get("id", "")), "page_name": str(page.get("name", page.get("id", ""))),
               "status": status, "metrics": {}, "issues": [], "collected_at": None, "demo": demo}
        for key, metadata in METRICS.items():
            row[key] = None
            row["metrics"][key] = {**metadata, "value": None, "status": "unavailable", "data_points": 0,
                                    "first_end_time": None, "last_end_time": None}
        return row

    @staticmethod
    def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, metadata in METRICS.items():
            included = [row["metrics"][key] for row in rows
                        if _integer(row.get(key)) and row["metrics"][key].get("source") == metadata["source"]
                        and row["metrics"][key].get("scope") == metadata["scope"]
                        and row["metrics"][key].get("status") in {"ok", "partial"}]
            partial = sum(item.get("status") == "partial" for item in included)
            count = len(included)
            result[key] = {**metadata, "value": sum(item["value"] for item in included) if count else None,
                           "available_pages": count, "total_pages": len(rows), "partial_pages": partial,
                           "status": "ok" if count == len(rows) and count and not partial else "partial" if count else "unavailable"}
        return result

    @staticmethod
    def _remaining_not_loaded(report: dict[str, Any]) -> None:
        for row in report["rows"]:
            if row.get("status") in {"queued", "loading"}:
                row["status"] = "not_loaded"

    def stop(self) -> dict[str, Any]:
        with self.state.lock:
            self._cancel.set()
            self._generation += 1
            if self._report is not None and self._report.get("state") == "running":
                self._report.update(state="cancelled", updated_at=self._now(), finished_at=self._now())
                self._remaining_not_loaded(self._report)
                self._report["summary"] = self._summary(self._report["rows"])
                self._save()
            return {"report": copy.deepcopy(self._report)}

    def invalidate(self) -> None:
        """Called under State.lock when credentials, Pages or demo mode change."""
        with self.state.lock:
            self._cancel.set()
            self._generation += 1
            self._report = None
            self._identity = (self.state.client, bool(self.state.demo), self._ids(), getattr(self.state, "session_revision", None))
            # Do not make a previous account's report reappear on next launch.
            with sqlite3.connect(self.state.db_path) as db:
                db.execute("DELETE FROM analytics_reports WHERE id='latest'")

    def close(self) -> None:
        with self.state.lock:
            self._closed = True
            self.stop()
            threads = list(self._threads)
        deadline = time.monotonic() + 2
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))

    def _check(self, generation: int, cancel: threading.Event) -> None:
        with self.state.lock:
            if (generation != self._generation or cancel.is_set() or self.state.shutdown.is_set() or self.state.connecting
                    or self._closed or not self._identity_matches()):
                raise AnalyticsCancelled()

    @staticmethod
    def _text(value: Any, tokens: list[str]) -> str:
        text = value if isinstance(value, str) else ""
        for token in tokens:
            text = text.replace(token, "[token đã ẩn]")
        return text[:1200]

    @staticmethod
    def _tokens(client: Any, pages: list[dict[str, Any]]) -> list[str]:
        tokens = [page.get("access_token") for page in pages]
        tokens.append(getattr(client, "user_token", None))
        tokens.extend(getattr(client, "_known_tokens", set()))
        return sorted({token for token in tokens if isinstance(token, str) and token}, key=len, reverse=True)

    def _public_row(self, raw: Any, page: dict[str, Any], demo: bool, tokens: list[str]) -> dict[str, Any]:
        row = self._empty_row(page, "error", demo=demo)
        row["page_name"] = self._text(row["page_name"], tokens)
        if not isinstance(raw, dict):
            row["issues"] = [{"metric": "all", "message": "Meta chưa trả về thống kê hợp lệ."}]
            return row
        row["collected_at"] = self._text(raw.get("collected_at"), tokens) or self._now()
        issues = raw.get("issues", [])
        if isinstance(issues, list):
            row["issues"] = [{"metric": issue.get("metric") if isinstance(issue.get("metric"), str) and issue.get("metric") in METRICS else "all",
                              "message": self._text(issue.get("message"), tokens)}
                             for issue in issues if isinstance(issue, dict)][:30]
        metrics = raw.get("metrics", {})
        metrics = metrics if isinstance(metrics, dict) else {}
        for key, metadata in METRICS.items():
            incoming = metrics.get(key, {})
            incoming = incoming if isinstance(incoming, dict) else {}
            item = row["metrics"][key]
            value = incoming.get("value", raw.get(key))
            status = incoming.get("status", "ok" if _integer(value) else "unavailable")
            status = status if isinstance(status, str) else "unavailable"
            source = incoming.get("source", metadata["source"])
            scope = incoming.get("scope", metadata["scope"])
            if source != metadata["source"] or scope != metadata["scope"]:
                row["issues"].append({"metric": key, "message": "Nguồn thống kê không khớp chỉ số; chưa cộng vào tổng."})
                status, value = "unavailable", None
            elif not _integer(value) or status not in {"ok", "partial"}:
                value = None
                status = status if status in {"unavailable", "error"} else "unavailable"
            item.update(value=value, status=status)
            item["data_points"] = incoming.get("data_points") if _integer(incoming.get("data_points")) else 0
            for end_time in ("first_end_time", "last_end_time"):
                item[end_time] = self._text(incoming.get(end_time), tokens) or None
            row[key] = value
        available = sum(_integer(row[key]) for key in METRICS)
        row["status"] = "ok" if available == len(METRICS) and all(item["status"] == "ok" for item in row["metrics"].values()) else "partial" if available else "error" if raw.get("status") == "error" else "partial"
        return row

    def _demo_row(self, page: dict[str, Any], index: int, days: int) -> dict[str, Any]:
        row = self._empty_row(page, "ok", demo=True)
        row["collected_at"] = self._now()
        values = {"views": (index + 1) * 170 * days, "engagements": (index + 1) * 8 * days,
                  "followers": 400 + index * 51, "new_followers": (index + 1) * days}
        for key, value in values.items():
            row[key] = value
            row["metrics"][key].update(value=value, status="ok", data_points=1 if key == "followers" else days)
        if index % 13 == 12:
            row = self._empty_row(page, "error", demo=True)
            row["collected_at"] = self._now()
            row["issues"] = [{"metric": "all", "message": "Ví dụ demo: Page chưa cấp quyền đọc thống kê."}]
        elif index % 7 == 6:
            row["status"] = "partial"
            row["views"] = None
            row["metrics"]["views"].update(value=None, status="unavailable", data_points=0)
            row["issues"] = [{"metric": "views", "message": "Ví dụ demo: chỉ số lượt xem chưa khả dụng."}]
        return row

    def _collect(self, generation: int, cancel: threading.Event, client: Any, demo: bool,
                 pages: list[dict[str, Any]], date_range: dict[str, Any]) -> None:
        tokens = self._tokens(client, pages)
        guarded = _SessionClient(client, lambda: self._check(generation, cancel)) if not demo else None
        try:
            for index, page in enumerate(pages):
                self._check(generation, cancel)
                with self.state.lock:
                    self._check(generation, cancel)
                    self._report["rows"][index]["status"] = "loading"
                    self._report["updated_at"] = self._now()
                try:
                    if demo:
                        if cancel.wait(0.01):
                            raise AnalyticsCancelled()
                        raw = self._demo_row(page, index, date_range["days"])
                    elif not page.get("access_token"):
                        raw = {"status": "error", "issues": [{"metric": "all", "message": "Fanpage chưa có Page Access Token để đọc thống kê."}]}
                    else:
                        # No State.lock is held while waiting for a publishing
                        # request or while reading Meta. Recheck once acquired.
                        with self.state.api_lock:
                            self._check(generation, cancel)
                            raw = self.fetcher(guarded, page, date_range["since"], date_range["until"],
                                               range_days=date_range["days"], date_preset=date_range["date_preset"])
                except AnalyticsCancelled:
                    raise
                except GraphError as exc:
                    raw = {"status": "error", "issues": [{"metric": "all", "message": self._text(str(exc), tokens)}]}
                except Exception:
                    raw = {"status": "error", "issues": [{"metric": "all", "message": "Chưa đọc được thống kê Fanpage; thử cập nhật lại."}]}
                row = self._public_row(raw, page, demo, tokens)
                with self.state.lock:
                    self._check(generation, cancel)
                    self._report["rows"][index] = row
                    self._report["completed_pages"] += 1
                    self._report["updated_at"] = self._now()
                    self._report["summary"] = self._summary(self._report["rows"])
                    self._save()
            with self.state.lock:
                self._check(generation, cancel)
                self._report.update(state="completed", updated_at=self._now(), finished_at=self._now())
                self._save()
        except AnalyticsCancelled:
            with self.state.lock:
                if generation == self._generation:
                    if not self._identity_matches():
                        self.invalidate()
                    elif self._report is not None and self._report.get("state") == "running":
                        self.stop()
        except Exception:
            # A storage failure does not justify returning made-up data or
            # keeping a report permanently "running".
            with self.state.lock:
                if generation == self._generation and self._report is not None:
                    self._cancel.set()
                    self._generation += 1
                    self._report.update(state="cancelled", updated_at=self._now(), finished_at=self._now())
                    self._remaining_not_loaded(self._report)
                    self._report["summary"] = self._summary(self._report["rows"])
        finally:
            with self.state.lock:
                self._threads.discard(threading.current_thread())
