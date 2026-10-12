"""Read-only, per-Page analytics with explicit sources and missing-data states.

Page Insights metric availability varies by API version and account. The three
daily names below are independent probes: an unavailable count is never replaced
by impressions, video-only views, Page likes, or a fabricated zero.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from meta_client import GraphClient, GraphError


class AnalyticsCancelled(Exception):
    """A caller's session/cancellation guard can stop all remaining reads."""


METRIC_SPECS = {
    "views": {"source": "page_media_view", "label": "Lượt xem nội dung", "scope": "range"},
    "engagements": {"source": "page_post_engagements", "label": "Lượt tương tác bài viết", "scope": "range"},
    "followers": {"source": "followers_count", "label": "Người theo dõi hiện tại", "scope": "current"},
    "new_followers": {"source": "page_daily_follows", "label": "Lượt theo dõi mới", "scope": "range"},
}
PRESET_DAYS = {"today": 1, "last_7d": 7, "last_30d": 30}
MAX_PAGES = 100
SECONDS_PER_DAY = 86_400


def fetch_page_analytics(
    client: GraphClient,
    page: dict[str, Any],
    since: int,
    until: int,
    *,
    range_days: int | None = None,
    date_preset: str | None = None,
) -> dict[str, Any]:
    """Fetch one current follower snapshot and three independent daily series.

    Native Meta presets are sent without competing since/until parameters. The
    day count measures coverage; daily end times belong to Meta, so buckets are
    not trimmed using an assumed local or UTC midnight. Read errors retain known
    partial data. Cancellation always propagates to the background collector.
    """
    expected_days = _validate_range(since, until, range_days, date_preset)
    page_id = str(page.get("id", "")) if isinstance(page, dict) else ""
    page_name = page.get("name") if isinstance(page, dict) else None
    row: dict[str, Any] = {
        "page_id": page_id,
        "page_name": page_name if isinstance(page_name, str) else page_id,
        "status": "error",
        "views": None,
        "engagements": None,
        "followers": None,
        "new_followers": None,
        "metrics": {key: _metric(key) for key in METRIC_SPECS},
        "issues": [],
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        page_id, token = client._page_auth(page)
        row["page_id"] = page_id
    except AnalyticsCancelled:
        raise
    except Exception as exc:
        for key in METRIC_SPECS:
            row["metrics"][key]["status"] = "error"
            _issue(row, key, _error_message(exc))
        return row

    stop_code: int | None = None
    followers = row["metrics"]["followers"]
    try:
        payload = client._request("GET", page_id, token, params={"fields": "followers_count"})
        count = _count(payload.get("followers_count")) if isinstance(payload, dict) else None
        if count is None:
            _issue(row, "followers", "Meta chưa trả về số người theo dõi hiện tại hợp lệ; không thay bằng lượt thích trang.")
        else:
            followers.update(value=count, status="ok", data_points=1)
    except AnalyticsCancelled:
        raise
    except Exception as exc:
        followers["status"] = _error_status(exc)
        _issue(row, "followers", _error_message(exc))
        stop_code = _stop_code(exc)

    for key in ("views", "engagements", "new_followers"):
        if stop_code is not None:
            row["metrics"][key]["status"] = "error" if stop_code == 190 else "unavailable"
            _issue(row, key, _error_message(GraphError("", code=stop_code)))
            continue
        metric, messages, stop_code = _daily_metric(
            client, page_id, token, key, since, until, expected_days, date_preset,
        )
        row["metrics"][key] = metric
        for message in messages:
            _issue(row, key, message)

    for key in METRIC_SPECS:
        row[key] = row["metrics"][key]["value"]
    if all(row[key] is None for key in METRIC_SPECS):
        row["status"] = "error"
    elif all(row["metrics"][key]["status"] == "ok" for key in METRIC_SPECS):
        row["status"] = "ok"
    else:
        row["status"] = "partial"
    return row


def _daily_metric(client, page_id, token, key, since, until, expected_days, date_preset):
    metric = _metric(key)
    source = metric["source"]
    params: dict[str, Any] = {"metric": source, "period": "day"}
    if date_preset is not None:
        params["date_preset"] = date_preset
    else:
        params.update(since=since, until=until)
    buckets: dict[datetime, int] = {}
    conflicts: set[datetime] = set()
    seen_cursors: set[str] = set()
    messages: list[str] = []
    error_status: str | None = None
    stop_code: int | None = None
    for page_number in range(MAX_PAGES):
        try:
            payload = client._request("GET", f"{page_id}/insights", token, params=dict(params))
        except AnalyticsCancelled:
            raise
        except Exception as exc:
            messages.append(_error_message(exc))
            error_status = _error_status(exc)
            stop_code = _stop_code(exc)
            break
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            messages.append("Meta trả về cấu trúc thống kê không hợp lệ; chưa cộng dữ liệu này.")
            error_status = "error"
            break
        series = payload["data"]
        matching = [item for item in series if isinstance(item, dict) and item.get("name") == source and item.get("period") == "day"]
        if len(matching) > 1:
            # A single-metric request should have one daily series per response.
            # Summing two matching entries can count the same series twice.
            messages.append("Meta trả về chuỗi chỉ số trùng lặp trong cùng phản hồi; chưa cộng các chuỗi này.")
        elif matching:
            values = matching[0].get("values")
            if not isinstance(values, list):
                messages.append("Meta chưa cung cấp chuỗi số đếm theo ngày; không cộng tổng hoặc dữ liệu phân nhóm khác.")
            else:
                for item in values:
                    count = _count(item.get("value")) if isinstance(item, dict) else None
                    end_time = _end_time(item.get("end_time")) if isinstance(item, dict) else None
                    if count is None or end_time is None:
                        messages.append("Có ngày thiếu số đếm hoặc mốc kết thúc hợp lệ; đã bỏ phần chưa xác định.")
                        continue
                    if end_time in conflicts:
                        continue
                    if end_time in buckets and buckets[end_time] != count:
                        del buckets[end_time]
                        conflicts.add(end_time)
                        messages.append("Meta trả về số đếm khác nhau cho cùng một ngày; đã bỏ ngày có xung đột.")
                    else:
                        buckets[end_time] = count
        elif series:
            messages.append("Meta chưa trả về đúng chỉ số và kỳ theo ngày; không cộng kỳ trọn đời hoặc chỉ số khác.")

        paging = payload.get("paging", {})
        next_url = paging.get("next") if isinstance(paging, dict) else None
        if not next_url:
            break
        try:
            parts = client._trusted_url(next_url, client.GRAPH_HOST)
            query = parse_qs(parts.query, keep_blank_values=True)
            cursors = query.get("after", [])
            if not cursors:
                # Insights also exposes temporal next links. Following their
                # since/until would leave the requested range/native preset.
                # Their path is unused: do not flag a complete report just
                # because Meta canonicalized a date-navigation link.
                break
            # Only the cursor is copied; requests always use the original
            # configured version, Page, metric and range. A returned version
            # or metric-specific edge must never replace those parameters.
            path_pattern = rf"/(?:v[1-9][0-9]?\.0/)?{re.escape(page_id)}/insights(?:/{re.escape(source)})?/?"
            if not re.fullmatch(path_pattern, parts.path):
                raise GraphError("Đường dẫn phân trang không hợp lệ.")
            if (len(cursors) != 1 or not cursors[0] or cursors[0] in seen_cursors
                    or any(ord(char) < 32 or ord(char) == 127 for char in cursors[0])):
                raise GraphError("Con trỏ phân trang không hợp lệ.")
            seen_cursors.add(cursors[0])
            params["after"] = cursors[0]
        except AnalyticsCancelled:
            raise
        except Exception:
            messages.append("Phân trang thống kê không hợp lệ; đã dừng để bảo vệ token và giữ nguyên khoảng đã chọn.")
            error_status = "error"
            break
        if page_number == MAX_PAGES - 1:
            messages.append("Đã chạm giới hạn phân trang; dữ liệu thống kê chưa đầy đủ.")
    ordered = sorted(buckets)
    overlapping = set()
    for left, right in zip(ordered, ordered[1:]):
        # Meta's daily boundary can move by an hour for DST. Shorter intervals
        # cannot describe two independent daily buckets; choosing either count
        # would be arbitrary, and summing both would inflate aggregate totals.
        if (right - left).total_seconds() < 23 * 3600:
            overlapping.update((left, right))
    if overlapping:
        for end_time in overlapping:
            del buckets[end_time]
        messages.append("Các mốc thống kê theo ngày chồng lấn; đã bỏ các ngày có khoảng thời gian không xác định.")
        ordered = sorted(buckets)
    if ordered:
        metric.update(
            value=sum(buckets.values()),
            data_points=len(ordered),
            first_end_time=ordered[0].isoformat(timespec="seconds"),
            last_end_time=ordered[-1].isoformat(timespec="seconds"),
        )
        if len(ordered) != expected_days:
            messages.append(f"Meta trả về {len(ordered)}/{expected_days} mốc ngày; dữ liệu có thể chưa cập nhật đầy đủ theo kỳ đã chọn.")
        if any((right - left).total_seconds() > 25 * 3600 for left, right in zip(ordered, ordered[1:])):
            messages.append("Chuỗi thống kê có khoảng trống giữa các ngày; tổng hiện chỉ gồm phần Meta đã trả về.")
        metric["status"] = "partial" if messages else "ok"
    else:
        metric["status"] = error_status or "unavailable"
        if not messages:
            messages.append("Meta chưa có dữ liệu cho chỉ số trong kỳ này; không coi dữ liệu thiếu là số 0.")
    return metric, list(dict.fromkeys(messages)), stop_code


def _metric(key: str) -> dict[str, Any]:
    return {
        **METRIC_SPECS[key],
        "value": None,
        "status": "unavailable",
        "data_points": 0,
        "first_end_time": None,
        "last_end_time": None,
    }


def _issue(row: dict[str, Any], key: str, message: str) -> None:
    issue = {"metric": key, "message": message}
    if issue not in row["issues"]:
        row["issues"].append(issue)


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _end_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is not None:
            return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    return None


def _error_status(exc: Exception) -> str:
    return "unavailable" if isinstance(exc, GraphError) and exc.code in (10, 100, 200) else "error"


def _stop_code(exc: Exception) -> int | None:
    return exc.code if isinstance(exc, GraphError) and exc.code in (10, 190, 200) else None


def _error_message(exc: Exception) -> str:
    # Even GraphError is reduced to known reasons here: arbitrary caller/proxy
    # exception messages and signed URLs must never enter analytics history.
    code = exc.code if isinstance(exc, GraphError) else None
    if code == 190:
        return "Page Access Token hết hạn hoặc không hợp lệ; hãy kết nối lại tài khoản để đọc thống kê."
    if code in (10, 200):
        return "Tài khoản hoặc Meta App thiếu quyền đọc dữ liệu Fanpage/Insights; kiểm tra pages_read_engagement và read_insights."
    if code == 100:
        return "Meta không cung cấp chỉ số này với phiên bản API, tham số hoặc quyền hiện tại."
    if code in (4, 17, 32, 613):
        return "Meta đang giới hạn số yêu cầu thống kê; hãy chờ rồi tải lại."
    return "Không lấy được dữ liệu từ Meta; kiểm tra kết nối và quyền tài khoản rồi tải lại."


def _validate_range(since: Any, until: Any, range_days: Any, date_preset: Any) -> int:
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (since, until)) or until <= since:
        raise ValueError("Khoảng thống kê cần hai mốc Unix dương và thời điểm kết thúc sau bắt đầu.")
    if date_preset is not None and (not isinstance(date_preset, str) or date_preset not in PRESET_DAYS):
        raise ValueError("Kỳ thống kê phải là today, last_7d hoặc last_30d.")
    expected = PRESET_DAYS[date_preset] if date_preset is not None else math.ceil((until - since) / SECONDS_PER_DAY)
    if range_days is not None:
        if not isinstance(range_days, int) or isinstance(range_days, bool) or not 1 <= range_days <= 90:
            raise ValueError("Số ngày thống kê phải từ 1 đến 90.")
        if date_preset is not None and range_days != expected:
            raise ValueError("Số ngày thống kê không khớp kỳ Meta đã chọn.")
        expected = range_days
    if not 1 <= expected <= 90 or until - since > 91 * SECONDS_PER_DAY:
        raise ValueError("Khoảng thống kê tối đa 90 ngày theo lịch.")
    return expected
