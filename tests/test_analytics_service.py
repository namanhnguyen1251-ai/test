import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from analytics_service import AnalyticsService, METRICS
from meta_client import GraphError
from page_analytics import AnalyticsCancelled, fetch_page_analytics


class FakeClient:
    user_token = "private-user-token"

    def __init__(self):
        self.calls = []
        self._known_tokens = {self.user_token}
        self.on_request = None

    def _request(self, method, path, token, **kwargs):
        self.calls.append((method, path, token))
        if self.on_request:
            self.on_request()
        return {"data": [], "followers_count": 10}

    def _page_auth(self, page):
        return page["id"], page["access_token"]


class FakeState:
    def __init__(self, path, count=1, connected=True, demo=False):
        self.lock = threading.RLock()
        self.api_lock = threading.RLock()
        self.shutdown = threading.Event()
        self.db_path = Path(path) / "history.sqlite3"
        self.client = FakeClient() if connected and not demo else None
        self.demo = demo
        self.connecting = False
        self.session_revision = 0
        self.posting = False
        self.pages = [{"id": str(index + 1), "name": f"Page {index + 1}",
                       "access_token": f"private-page-token-{index + 1}",
                       "tasks": ["ANALYZE"], "can_publish": False} for index in range(count)] if connected or demo else []

    def active(self):
        return self.posting


def public_row(page, **values):
    row = {"page_id": page["id"], "page_name": page["name"], "status": "ok", "metrics": {},
           "issues": [], "collected_at": "2026-10-12T00:00:00+00:00"}
    for key, metadata in METRICS.items():
        value = values.get(key, 10)
        row[key] = value
        row["metrics"][key] = {**metadata, "value": value, "status": "ok" if value is not None else "unavailable",
                                "data_points": 1, "first_end_time": None, "last_end_time": None}
    return row


class AnalyticsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = FakeState(self.temp.name)
        self.services = []
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for service in self.services:
            service.close()
        self.temp.cleanup()

    def manager(self, fetcher=None, state=None):
        service = AnalyticsService(state or self.state, fetcher=fetcher or (lambda client, page, *args, **kwargs: public_row(page)),
                                   clock=lambda: 1791763200)
        self.services.append(service)
        return service

    def finish(self, service):
        service._thread.join(timeout=3)
        self.assertFalse(service._thread.is_alive())
        return service.snapshot()["report"]

    def blocker(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def fetch(client, page, *args, **kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return public_row(page)

        return fetch, started, release

    def test_all_fifty_pages_collected_even_without_publish_permission(self):
        self.state.pages = FakeState(self.temp.name, count=50).pages
        seen = []

        def fetch(client, page, *args, **kwargs):
            seen.append(page["id"])
            return public_row(page)

        service = self.manager(fetch)
        initial = service.refresh()["report"]
        self.assertEqual(initial["total_pages"], 50)
        self.assertEqual(len(initial["rows"]), 50)
        self.assertEqual(initial["completed_pages"], 0)
        self.assertTrue(all(row["views"] is None for row in initial["rows"]))
        report = self.finish(service)
        self.assertEqual(seen, [str(number) for number in range(1, 51)])
        self.assertEqual(report["completed_pages"], 50)
        self.assertEqual(report["summary"]["views"]["value"], 500)

    def test_presets_forward_native_meta_ranges(self):
        seen = []

        def fetch(client, page, since, until, **kwargs):
            seen.append((since, until, kwargs))
            return public_row(page)

        service = self.manager(fetch)
        for preset, days, native in (("today", 1, "today"), ("7days", 7, "last_7d"), ("30days", 30, "last_30d")):
            service.refresh(preset)
            report = self.finish(service)
            self.assertEqual(report["range"]["days"], days)
            self.assertEqual(report["range"]["date_preset"], native)
            self.assertEqual(seen[-1][2], {"range_days": days, "date_preset": native})
        # At exact UTC midnight the diagnostic bounds remain valid even
        # though the API uses its own native preset instead of these bounds.
        self.assertEqual(seen[0][1] - seen[0][0], 1)
        self.assertEqual(seen[1][1] - seen[1][0], 6 * 86400)

    def test_default_is_thirty_days_and_unsupported_preset_rejected(self):
        service = self.manager()
        self.assertEqual(service.refresh()["report"]["preset"], "30days")
        self.finish(service)
        for value in ("month", None, 7, [], "last_7d"):
            with self.assertRaises(ValueError):
                service.refresh(value)

    def test_today_at_exact_midnight_reaches_real_fetcher_with_valid_bounds(self):
        service = self.manager(fetch_page_analytics)
        initial = service.refresh("today")["report"]
        self.assertLess(initial["range"]["since"], initial["range"]["until"])
        report = self.finish(service)
        self.assertEqual(len(self.state.client.calls), 4)
        self.assertEqual(report["rows"][0]["followers"], 10)

    def test_one_failure_does_not_drop_other_pages(self):
        self.state.pages = FakeState(self.temp.name, count=3).pages

        def fetch(client, page, *args, **kwargs):
            if page["id"] == "2":
                raise GraphError("Thiếu quyền đọc thống kê")
            return public_row(page, views=5)

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertEqual([row["status"] for row in report["rows"]], ["ok", "error", "ok"])
        self.assertEqual(report["summary"]["views"], {**METRICS["views"], "value": 10, "available_pages": 2,
                                                       "total_pages": 3, "partial_pages": 0, "status": "partial"})

    def test_page_without_token_has_error_row_without_api_read(self):
        self.state.pages = FakeState(self.temp.name, count=2).pages
        self.state.pages[0]["access_token"] = ""
        seen = []

        def fetch(client, page, *args, **kwargs):
            seen.append(page["id"])
            return public_row(page)

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertEqual(seen, ["2"])
        self.assertEqual(report["rows"][0]["status"], "error")
        self.assertIn("Page Access Token", report["rows"][0]["issues"][0]["message"])

    def test_real_zero_is_distinct_from_unknown(self):
        service = self.manager(lambda client, page, *args, **kwargs: public_row(page, views=0, engagements=None))
        service.refresh()
        report = self.finish(service)
        self.assertEqual(report["summary"]["views"]["value"], 0)
        self.assertEqual(report["summary"]["views"]["status"], "ok")
        self.assertIsNone(report["summary"]["engagements"]["value"])
        self.assertEqual(report["summary"]["engagements"]["available_pages"], 0)

    def test_bool_negative_and_fraction_are_not_counted(self):
        service = self.manager(lambda client, page, *args, **kwargs: public_row(page, views=True, engagements=-1, followers=1.5))
        service.refresh()
        report = self.finish(service)
        for key in ("views", "engagements", "followers"):
            self.assertIsNone(report["rows"][0][key])
            self.assertIsNone(report["summary"][key]["value"])

    def test_followers_snapshot_counted_once_and_not_multiplied_by_days(self):
        def fetch(client, page, *args, **kwargs):
            row = public_row(page, followers=25, new_followers=40)
            row["metrics"]["followers"]["data_points"] = 30
            return row

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertEqual(report["summary"]["followers"]["value"], 25)
        self.assertEqual(report["summary"]["followers"]["scope"], "current")
        self.assertEqual(report["summary"]["new_followers"]["value"], 40)
        self.assertEqual(report["summary"]["new_followers"]["source"], "page_daily_follows")

    def test_partial_subtotal_maintains_coverage_and_metadata(self):
        def fetch(client, page, *args, **kwargs):
            row = public_row(page, views=5)
            row["metrics"]["views"].update(status="partial", data_points=2,
                                           first_end_time="2026-10-10T00:00:00+00:00",
                                           last_end_time="2026-10-11T00:00:00+00:00")
            return row

        service = self.manager(fetch)
        service.refresh("7days")
        report = self.finish(service)
        self.assertEqual(report["summary"]["views"]["value"], 5)
        self.assertEqual(report["summary"]["views"]["partial_pages"], 1)
        self.assertEqual(report["summary"]["views"]["status"], "partial")
        self.assertEqual(report["rows"][0]["metrics"]["views"]["data_points"], 2)

    def test_impressions_cannot_be_added_to_views(self):
        self.state.pages = FakeState(self.temp.name, count=2).pages

        def fetch(client, page, *args, **kwargs):
            row = public_row(page, views=100)
            if page["id"] == "1":
                row["metrics"]["views"]["source"] = "page_impressions"
            return row

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertIsNone(report["rows"][0]["views"])
        self.assertEqual(report["summary"]["views"]["value"], 100)
        self.assertEqual(report["summary"]["views"]["available_pages"], 1)
        self.assertTrue(report["rows"][0]["issues"])

    def test_refresh_is_async_with_all_rows_visible_during_progress(self):
        fetch, started, release = self.blocker()
        self.state.pages = FakeState(self.temp.name, count=3).pages
        service = self.manager(fetch)
        result = service.refresh()["report"]
        self.assertTrue(started.wait(1))
        live = service.snapshot()["report"]
        self.assertEqual(result["state"], "running")
        self.assertEqual(live["completed_pages"], 0)
        self.assertEqual(live["rows"][0]["status"], "loading")
        self.assertEqual(live["rows"][1]["status"], "queued")
        self.assertIsNone(live["summary"]["views"]["value"])
        release.set()
        self.assertEqual(self.finish(service)["state"], "completed")

    def test_simultaneous_refresh_is_rejected(self):
        fetch, started, release = self.blocker()
        service = self.manager(fetch)
        service.refresh()
        self.assertTrue(started.wait(1))
        with self.assertRaises(ValueError):
            service.refresh("today")
        release.set()
        self.finish(service)

    def test_stop_marks_only_unloaded_rows_and_preserves_completed_data(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        self.state.pages = FakeState(self.temp.name, count=3).pages

        def fetch(client, page, *args, **kwargs):
            if page["id"] == "2":
                started.set()
                release.wait(2)
            return public_row(page, views=100)

        service = self.manager(fetch)
        service.refresh()
        self.assertTrue(started.wait(1))
        stopped = service.stop()["report"]
        self.assertEqual(stopped["state"], "cancelled")
        self.assertEqual(stopped["completed_pages"], 1)
        self.assertEqual([row["status"] for row in stopped["rows"]], ["ok", "not_loaded", "not_loaded"])
        self.assertEqual(stopped["summary"]["views"]["value"], 100)
        self.assertIsNone(stopped["rows"][1]["views"])
        release.set()
        self.assertEqual(self.finish(service), stopped)

    def test_inflight_response_cannot_repopulate_report_after_account_switch(self):
        fetch, started, release = self.blocker()
        service = self.manager(fetch)
        service.refresh()
        self.assertTrue(started.wait(1))
        with self.state.lock:
            self.state.client = FakeClient()
            self.state.pages = [{"id": "999", "name": "New account", "access_token": "other-secret"}]
            self.state.session_revision += 1
            service.invalidate()
        release.set()
        self.assertIsNone(self.finish(service))
        with sqlite3.connect(self.state.db_path) as db:
            self.assertIsNone(db.execute("SELECT payload FROM analytics_reports WHERE id='latest'").fetchone())

    def test_per_request_guard_stops_next_read_after_session_change(self):
        client = self.state.client
        service = None

        def change():
            with self.state.lock:
                self.state.client = FakeClient()
                self.state.session_revision += 1
                service.invalidate()

        client.on_request = change

        def fetch(proxy, page, *args, **kwargs):
            proxy._request("GET", page["id"], page["access_token"])
            proxy._request("GET", page["id"] + "/insights", page["access_token"])
            return public_row(page)

        service = self.manager(fetch)
        service.refresh()
        self.assertIsNone(self.finish(service))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.state.client.calls, [])

    def test_waiting_api_lock_rechecks_session_before_using_old_token(self):
        called = threading.Event()

        def fetch(client, page, *args, **kwargs):
            called.set()
            return public_row(page)

        service = self.manager(fetch)
        with self.state.api_lock:
            service.refresh()
            # Acquiring State.lock here also verifies the worker never waits
            # for api_lock while holding the state lock.
            with self.state.lock:
                self.state.client = FakeClient()
                self.state.session_revision += 1
                service.invalidate()
        self.assertIsNone(self.finish(service))
        self.assertFalse(called.is_set())

    def test_reconnect_intent_cancels_at_next_request_boundary(self):
        client = self.state.client

        def reconnect():
            with self.state.lock:
                self.state.connecting = True

        client.on_request = reconnect

        def fetch(proxy, page, *args, **kwargs):
            proxy._request("GET", page["id"], page["access_token"])
            proxy._request("GET", page["id"] + "/insights", page["access_token"])
            return public_row(page)

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report["state"], "cancelled")
        self.assertIsNone(report["rows"][0]["views"])
        self.state.connecting = False

    def test_page_ids_mutation_alone_invalidates_current_report(self):
        service = self.manager()
        service.refresh()
        self.finish(service)
        self.state.pages[0]["id"] = "2"
        self.assertIsNone(service.snapshot()["report"])

    def test_revision_change_with_same_client_and_pages_invalidates(self):
        service = self.manager()
        service.refresh()
        self.finish(service)
        self.state.session_revision += 1
        self.assertIsNone(service.snapshot()["report"])

    def test_no_tokens_in_snapshot_or_persistent_report_even_if_error_echoes(self):
        self.state.pages = FakeState(self.temp.name, count=2).pages

        def fetch(client, page, *args, **kwargs):
            if page["id"] == "1":
                raise GraphError("Denied private-user-token private-page-token-2")
            row = public_row(page)
            row["access_token"] = page["access_token"]
            row["issues"] = [{"metric": "views", "message": "private-page-token-1 private-user-token"}]
            return row

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        rendered = json.dumps(report, ensure_ascii=False)
        with sqlite3.connect(self.state.db_path) as db:
            payload = db.execute("SELECT payload FROM analytics_reports WHERE id='latest'").fetchone()[0]
        for secret in ("private-user-token", "private-page-token-1", "private-page-token-2"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, payload)
        self.assertNotIn("access_token", rendered)
        self.assertIn("token đã ẩn", rendered)

    def test_snapshot_is_independent_copy(self):
        service = self.manager()
        service.refresh()
        report = self.finish(service)
        report["rows"][0]["views"] = 999
        report["summary"]["views"]["value"] = 999
        self.assertEqual(service.snapshot()["report"]["rows"][0]["views"], 10)

    def test_malformed_fetcher_row_cannot_leak_token_in_page_name(self):
        self.state.pages[0]["name"] = "Page private-user-token private-page-token-1"
        service = self.manager(lambda *args, **kwargs: None)
        initial = service.refresh()["report"]
        report = self.finish(service)
        self.assertEqual(report["rows"][0]["status"], "error")
        with sqlite3.connect(self.state.db_path) as db:
            payload = db.execute("SELECT payload FROM analytics_reports WHERE id='latest'").fetchone()[0]
        for output in (json.dumps(initial), json.dumps(report), payload):
            self.assertNotIn("private-user-token", output)
            self.assertNotIn("private-page-token-1", output)

    def test_persisted_snapshot_is_cached_after_restart_without_connection(self):
        service = self.manager()
        service.refresh()
        report = self.finish(service)
        restarted = self.manager(state=FakeState(self.temp.name, connected=False))
        cached = restarted.snapshot()["report"]
        self.assertEqual(cached["rows"], report["rows"])
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["state"], "completed")

    def test_restart_cancels_interrupted_rows_without_inventing_zeros(self):
        fetch, started, release = self.blocker()
        service = self.manager(fetch)
        service.refresh()
        self.assertTrue(started.wait(1))
        restarted = self.manager(state=FakeState(self.temp.name, connected=False))
        report = restarted.snapshot()["report"]
        self.assertEqual(report["state"], "cancelled")
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["rows"][0]["status"], "not_loaded")
        self.assertIsNone(report["rows"][0]["views"])
        self.assertIsNone(report["summary"]["views"]["value"])
        release.set()
        self.finish(service)

    def test_new_connected_session_does_not_see_previous_cache(self):
        service = self.manager()
        service.refresh()
        self.finish(service)
        self.assertIsNone(self.manager(state=FakeState(self.temp.name)).snapshot()["report"])
        self.assertIsNone(self.manager(state=FakeState(self.temp.name, demo=True)).snapshot()["report"])

    def test_connect_after_cached_snapshot_clears_cached_account(self):
        service = self.manager()
        service.refresh()
        self.finish(service)
        disconnected = FakeState(self.temp.name, connected=False)
        restarted = self.manager(state=disconnected)
        self.assertIsNotNone(restarted.snapshot()["report"])
        disconnected.client = FakeClient()
        disconnected.pages = copy.deepcopy(self.state.pages)
        self.assertIsNone(restarted.snapshot()["report"])

    def test_demo_uses_every_page_without_network_and_marks_missing_values(self):
        self.state.client = None
        self.state.demo = True
        self.state.pages = FakeState(self.temp.name, count=50, demo=True).pages
        called = []
        service = self.manager(lambda *args, **kwargs: called.append(1))
        service.refresh("7days")
        report = self.finish(service)
        self.assertEqual(called, [])
        self.assertTrue(report["demo"])
        self.assertEqual(len(report["rows"]), 50)
        self.assertTrue(all(row["demo"] for row in report["rows"]))
        self.assertEqual(report["rows"][6]["status"], "partial")
        self.assertEqual(report["rows"][12]["status"], "error")
        self.assertIsNone(report["rows"][12]["followers"])
        self.state.demo = False
        self.state.client = FakeClient()
        self.assertIsNone(service.snapshot()["report"])

    def test_restarted_refresh_cannot_be_overwritten_by_cancelled_worker(self):
        fetch, started, release = self.blocker()
        service = self.manager(fetch)
        old_id = service.refresh()["report"]["id"]
        self.assertTrue(started.wait(1))
        old_thread = service._thread
        service.stop()
        service.fetcher = lambda client, page, *args, **kwargs: public_row(page, views=777)
        new_id = service.refresh("today")["report"]["id"]
        release.set()
        report = self.finish(service)
        old_thread.join(2)
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(report["id"], new_id)
        self.assertEqual(report["rows"][0]["views"], 777)
        self.assertEqual(report["preset"], "today")

    def test_cancellation_exception_does_not_become_page_error(self):
        service = self.manager(lambda *args, **kwargs: (_ for _ in ()).throw(AnalyticsCancelled()))
        service.refresh()
        report = self.finish(service)
        self.assertEqual(report["state"], "cancelled")
        self.assertEqual(report["rows"][0]["status"], "not_loaded")
        self.assertEqual(report["completed_pages"], 0)

    def test_read_only_guard_never_sends_post(self):
        def fetch(client, page, *args, **kwargs):
            client._request("POST", page["id"] + "/feed", page["access_token"])
            return public_row(page)

        service = self.manager(fetch)
        service.refresh()
        report = self.finish(service)
        self.assertEqual(self.state.client.calls, [])
        self.assertEqual(report["rows"][0]["status"], "error")

    def test_missing_connection_connecting_posting_and_closed_are_rejected(self):
        service = self.manager()
        for field in ("connecting", "posting"):
            setattr(self.state, field, True)
            with self.assertRaises(ValueError):
                service.refresh()
            setattr(self.state, field, False)
        self.state.pages = []
        with self.assertRaises(ValueError):
            service.refresh()
        service.close()
        with self.assertRaises(ValueError):
            service.refresh()


if __name__ == "__main__":
    unittest.main()
