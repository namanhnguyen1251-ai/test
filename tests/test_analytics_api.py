import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from app import State, make_server
from meta_client import GraphClient, GraphError


class AnalyticsMetaClient:
    GRAPH_HOST = "graph.facebook.com"

    def __init__(self, token, version):
        self.user_token = token
        self.graph_version = version
        self.calls = []
        self.temporal_navigation = False

    def list_pages(self):
        return [{"id": "123", "name": "Trang đăng bài", "access_token": "private-page-one", "tasks": ["CREATE_CONTENT"]},
                {"id": "456", "name": "Trang chỉ phân tích", "access_token": "private-page-two", "tasks": ["ANALYZE"]}]

    def _page_auth(self, page):
        return page["id"], page["access_token"]

    def _trusted_url(self, url, host):
        return GraphClient._trusted_url(url, host)

    def _request(self, method, path, token, params=None, **kwargs):
        self.calls.append((method, path, params))
        if path in {"123", "456"}:
            return {"followers_count": 10 if path == "123" else 0}
        if path == "456/insights" and params["metric"] == "page_media_view" and not self.temporal_navigation:
            raise GraphError("private-page-two must not leak", code=100)
        days = {"today": 1, "last_7d": 7, "last_30d": 30}[params["date_preset"]]
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {"data": [{"name": params["metric"], "period": "day", "values": [
            {"value": 1, "end_time": (end - timedelta(days=index)).isoformat()} for index in range(days)]}]}
        if self.temporal_navigation:
            page_id = path.split('/')[0]
            payload['paging'] = {'next': f'https://graph.facebook.com/v25.0/{page_id}/insights/{params["metric"]}?since=9999999999&until=99999999999&access_token=private-page-one'}
        return payload


class AnalyticsAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(self.folder.name, client_factory=AnalyticsMetaClient)
        self.state.connect("private-user-token", "v24.0")
        self.server = make_server(0, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.state.close()
        self.folder.cleanup()

    def call(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, headers={
            **({"Content-Type": "application/json", "X-CSRF-Token": self.state.csrf} if payload is not None else {}),
            **(headers or {})})
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def refresh(self, preset="30days"):
        code, response = self.call("/api/analytics/refresh", {"preset": preset})
        self.assertEqual(code, 200, response)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, response = self.call("/api/analytics")
            if response["report"]["state"] != "running":
                return response["report"]
            time.sleep(0.01)
        self.fail("Analytics did not complete")

    def test_get_does_not_start_meta_reads_and_bootstrap_has_revision(self):
        code, response = self.call("/api/analytics")
        self.assertEqual(code, 200)
        self.assertIsNone(response["report"])
        self.assertEqual(self.state.client.calls, [])
        self.assertEqual(self.call("/api/bootstrap")[1]["session_revision"], 1)

    def test_all_pages_including_read_only_page_are_collected(self):
        report = self.refresh()
        self.assertEqual(report["total_pages"], 2)
        self.assertEqual(report["completed_pages"], 2)
        self.assertEqual([row["page_id"] for row in report["rows"]], ["123", "456"])
        self.assertEqual(report["rows"][0]["views"], 30)
        self.assertIsNone(report["rows"][1]["views"])
        self.assertEqual(report["rows"][1]["followers"], 0)
        self.assertEqual(report["summary"]["views"]["available_pages"], 1)
        self.assertEqual(report["summary"]["followers"]["value"], 10)
        for method, _, _ in self.state.client.calls:
            self.assertEqual(method, "GET")
        for token in ("private-user-token", "private-page-one", "private-page-two"):
            self.assertNotIn(token, json.dumps(report))
            self.assertNotIn(token.encode(), self.state.db_path.read_bytes())

    def test_today_seven_and_thirty_use_meta_presets_but_snapshot_not_multiplied(self):
        for preset, days, expected in [("today", 1, "today"), ("7days", 7, "last_7d"), ("30days", 30, "last_30d")]:
            report = self.refresh(preset)
            self.assertEqual(report["range"]["date_preset"], expected)
            self.assertEqual(report["rows"][0]["engagements"], days)
            self.assertEqual(report["rows"][0]["followers"], 10)
            self.assertEqual(report["rows"][0]["metrics"]["followers"]["scope"], "current")

    def test_canonical_date_navigation_does_not_make_complete_reports_partial(self):
        self.state.client.temporal_navigation = True
        for preset, days in [('today', 1), ('7days', 7), ('30days', 30)]:
            report = self.refresh(preset)
            self.assertEqual(report['state'], 'completed')
            self.assertEqual(report['completed_pages'], 2)
            self.assertTrue(all(row['status'] == 'ok' and not row['issues'] for row in report['rows']))
            self.assertEqual(report['summary']['views']['status'], 'ok')
            self.assertEqual(report['summary']['views']['value'], 2 * days)
            self.assertEqual(report['summary']['followers']['value'], 10)
            for row in report['rows']:
                self.assertEqual(row['metrics']['views']['data_points'], days)
            self.assertNotIn('private-page-one', json.dumps(report))
            self.assertNotIn('9999999999', json.dumps(report))

    def test_refresh_needs_csrf_and_foreign_origin_is_blocked(self):
        for headers in [{"X-CSRF-Token": "wrong"}, {"Origin": "https://other.example"}]:
            self.assertEqual(self.call("/api/analytics/refresh", {}, headers)[0], 403)
        self.assertIsNone(self.state.analytics)

    def test_demo_switch_and_disconnect_clear_prior_report(self):
        self.refresh()
        self.state.connect_demo()
        self.assertIsNone(self.call("/api/analytics")[1]["report"])
        demo = self.refresh("7days")
        self.assertEqual(len(demo["rows"]), 50)
        self.assertTrue(demo["demo"])
        self.state.disconnect()
        self.assertIsNone(self.call("/api/analytics")[1]["report"])
        self.assertEqual(self.call("/api/analytics/refresh", {})[0], 400)

    def test_completed_cache_reopens_offline_with_explicit_cached_label(self):
        report = self.refresh()
        self.state.close()
        reopened = State(self.folder.name, client_factory=AnalyticsMetaClient)
        try:
            saved = reopened.get_analytics().snapshot()["report"]
            self.assertTrue(saved["cached"])
            self.assertEqual(saved["id"], report["id"])
            self.assertIsNone(reopened.client)
            reopened.connect("another-user-token", "v24.0")
            self.assertIsNone(reopened.get_analytics().snapshot()["report"])
        finally:
            reopened.close()

    def test_invalid_preset_does_not_trigger_reads(self):
        self.assertEqual(self.call("/api/analytics/refresh", {"preset": "all-time"})[0], 400)
        self.assertEqual(self.state.client.calls, [])


if __name__ == "__main__":
    unittest.main()
