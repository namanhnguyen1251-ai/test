import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from meta_client import GraphClient, GraphError
from page_analytics import AnalyticsCancelled, METRIC_SPECS, fetch_page_analytics


PAGE = {"id": "123", "name": "Page thử nghiệm", "access_token": "private-page-token"}
SINCE = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
DAY = 86400


def series(source, values=(10, 20), times=None, period="day"):
    if times is None:
        times = [(datetime(2026, 10, 2, 7, tzinfo=timezone.utc) + timedelta(days=index)).isoformat() for index in range(len(values))]
    return {"data": [{"name": source, "period": period, "values": [{"value": value, "end_time": end_time} for value, end_time in zip(values, times)]}]}


class Client:
    GRAPH_HOST = GraphClient.GRAPH_HOST
    graph_version = "v24.0"

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler
        self.auth_error = None

    def _page_auth(self, page):
        if self.auth_error:
            raise self.auth_error
        return page["id"], page["access_token"]

    def _trusted_url(self, url, host):
        return GraphClient._trusted_url(url, host)

    def _request(self, method, path, token, *, params):
        if method != "GET":
            raise AssertionError("Analytics must never issue a write")
        self.calls.append({"method": method, "path": path, "token": token, "params": dict(params)})
        if self.handler is not None:
            response = self.handler(path, params)
            if response is not None:
                if isinstance(response, Exception):
                    raise response
                return response
        if path == "123":
            return {"followers_count": 42, "fan_count": 9999}
        metric = params["metric"]
        values = {"page_media_view": (10, 20), "page_post_engagements": (3, 4), "page_daily_follows": (1, 2)}[metric]
        return series(metric, values)


class PageAnalyticsTests(unittest.TestCase):
    def fetch(self, client, **kwargs):
        return fetch_page_analytics(client, PAGE, SINCE, SINCE + DAY * 2, range_days=2, **kwargs)

    def assert_safe(self, row):
        output = json.dumps(row, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("private-page-token", output)
        self.assertNotIn("private-user-token", output)
        self.assertNotIn("attacker.example", output)

    def test_scalar_daily_counts_and_current_followers_have_distinct_sources(self):
        client = Client()
        row = self.fetch(client)
        self.assertEqual(row["status"], "ok")
        self.assertEqual({key: row[key] for key in METRIC_SPECS}, {"views": 30, "engagements": 7, "followers": 42, "new_followers": 3})
        self.assertEqual(row["metrics"]["followers"]["scope"], "current")
        self.assertEqual(row["metrics"]["followers"]["source"], "followers_count")
        self.assertEqual(row["metrics"]["followers"]["data_points"], 1)
        self.assertIsNone(row["metrics"]["followers"]["first_end_time"])
        for key in ("views", "engagements", "new_followers"):
            self.assertEqual(row["metrics"][key]["scope"], "range")
            self.assertEqual(row["metrics"][key]["data_points"], 2)
        self.assertEqual(row["issues"], [])
        self.assertEqual(datetime.fromisoformat(row["collected_at"]).tzinfo, timezone.utc)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all(call["method"] == "GET" for call in client.calls))

    def test_explicit_ranges_send_one_metric_per_insights_read(self):
        client = Client()
        self.fetch(client)
        self.assertEqual(client.calls[0]["params"], {"fields": "followers_count"})
        for call in client.calls[1:]:
            self.assertEqual(call["path"], "123/insights")
            self.assertEqual(call["params"]["period"], "day")
            self.assertEqual(call["params"]["since"], SINCE)
            self.assertEqual(call["params"]["until"], SINCE + DAY * 2)
            self.assertNotIn(",", call["params"]["metric"])
            self.assertNotIn("access_token", call["params"])

    def test_native_presets_do_not_mix_since_until_parameters(self):
        for preset, days in (("today", 1), ("last_7d", 7), ("last_30d", 30)):
            with self.subTest(preset=preset):
                client = Client()
                fetch_page_analytics(client, PAGE, SINCE, SINCE + days * DAY, date_preset=preset, range_days=days)
                for call in client.calls[1:]:
                    self.assertEqual(call["params"]["date_preset"], preset)
                    self.assertNotIn("since", call["params"])
                    self.assertNotIn("until", call["params"])

    def test_native_preset_derives_expected_coverage_if_range_days_omitted(self):
        client = Client()
        row = fetch_page_analytics(client, PAGE, SINCE, SINCE + 7 * DAY, date_preset="last_7d")
        self.assertEqual(row["metrics"]["views"]["status"], "partial")
        self.assertTrue(any("2/7" in issue["message"] for issue in row["issues"]))

    def test_unsupported_metric_is_null_and_other_metrics_still_run(self):
        client = Client(lambda path, params: GraphError("private-page-token", code=100) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertIsNone(row["views"])
        self.assertEqual(row["metrics"]["views"]["status"], "unavailable")
        self.assertEqual(row["metrics"]["views"]["source"], "page_media_view")
        self.assertEqual(row["engagements"], 7)
        self.assertEqual(row["new_followers"], 3)
        self.assertEqual(row["status"], "partial")
        self.assertEqual(len(client.calls), 4)
        self.assert_safe(row)

    def test_follower_snapshot_does_not_fall_back_to_likes(self):
        client = Client(lambda path, params: {"fan_count": 9999} if path == "123" else None)
        row = self.fetch(client)
        self.assertIsNone(row["followers"])
        self.assertEqual(row["metrics"]["followers"]["status"], "unavailable")
        self.assertEqual(row["status"], "partial")

    def test_true_zero_counts_are_preserved_as_zero(self):
        client = Client(lambda path, params: {"followers_count": 0} if path == "123" else series(params["metric"], (0, 0)))
        row = self.fetch(client)
        self.assertEqual(row["status"], "ok")
        self.assertTrue(all(row[key] == 0 for key in METRIC_SPECS))

    def test_missing_data_is_not_zero(self):
        client = Client(lambda path, params: {"followers_count": 2} if path == "123" else {"data": []})
        row = self.fetch(client)
        self.assertEqual(row["status"], "partial")
        for key in ("views", "engagements", "new_followers"):
            self.assertIsNone(row[key])
            self.assertEqual(row["metrics"][key]["status"], "unavailable")

    def test_all_missing_values_make_row_error(self):
        client = Client(lambda path, params: {} if path == "123" else {"data": []})
        row = self.fetch(client)
        self.assertEqual(row["status"], "error")
        self.assertTrue(all(row[key] is None for key in METRIC_SPECS))

    def test_unsupported_follower_field_does_not_block_independent_insights(self):
        client = Client(lambda path, params: GraphError("private-page-token", code=100) if path == "123" else None)
        row = self.fetch(client)
        self.assertIsNone(row["followers"])
        self.assertEqual(row["views"], 30)
        self.assertEqual(len(client.calls), 4)

    def test_invalid_token_stops_remaining_page_reads(self):
        client = Client(lambda path, params: GraphError("private-user-token private-page-token", code=190))
        row = self.fetch(client)
        self.assertEqual(row["status"], "error")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(metric["status"] == "error" for metric in row["metrics"].values()))
        self.assert_safe(row)

    def test_insights_permission_error_stops_repeating_identical_failures(self):
        client = Client(lambda path, params: GraphError("private-page-token", code=200) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["followers"], 42)
        self.assertEqual(len(client.calls), 2)
        for key in ("views", "engagements", "new_followers"):
            self.assertEqual(row["metrics"][key]["status"], "unavailable")
        self.assert_safe(row)

    def test_unknown_exception_text_never_enters_result(self):
        client = Client(lambda path, params: RuntimeError("https://attacker.example?access_token=private-page-token private-user-token") if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["metrics"]["views"]["status"], "error")
        self.assertEqual(row["engagements"], 7)
        self.assert_safe(row)

    def test_rate_limit_is_not_retried(self):
        client = Client(lambda path, params: GraphError("private-page-token", code=613) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["metrics"]["views"]["status"], "error")
        self.assertEqual(sum(call["params"].get("metric") == "page_media_view" for call in client.calls), 1)

    def test_daily_scalar_validation_rejects_bool_breakdown_negative_and_nonfinite(self):
        for invalid in (True, False, -1, math.nan, math.inf, 1.5, "123", {"other": 8}, None):
            with self.subTest(value_type=type(invalid).__name__):
                client = Client(lambda path, params: series("page_media_view", (invalid, invalid)) if params.get("metric") == "page_media_view" else None)
                row = self.fetch(client)
                self.assertIsNone(row["views"])
                self.assertEqual(row["metrics"]["views"]["status"], "unavailable")
                self.assert_safe(row)

    def test_valid_integer_floats_are_counts_but_snapshot_bool_is_not(self):
        client = Client(lambda path, params: {"followers_count": True} if path == "123" else series(params["metric"], (3.0, 4.0)))
        row = self.fetch(client)
        self.assertEqual(row["views"], 7)
        self.assertIsNone(row["followers"])

    def test_partial_series_sums_only_valid_known_days(self):
        client = Client(lambda path, params: series("page_media_view", (10, None)) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 10)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")
        self.assertEqual(row["metrics"]["views"]["data_points"], 1)

    def test_lifetime_and_unique_period_counts_are_not_summed(self):
        for period in ("lifetime", "week", "days_28", "total_over_range"):
            with self.subTest(period=period):
                client = Client(lambda path, params: series("page_media_view", (10, 20), period=period) if params.get("metric") == "page_media_view" else None)
                row = self.fetch(client)
                self.assertIsNone(row["views"])
        client = Client(lambda path, params: series("page_engaged_users", (10, 20)) if params.get("metric") == "page_post_engagements" else None)
        self.assertIsNone(self.fetch(client)["engagements"])

    def test_total_value_is_not_misread_as_supported_page_daily_series(self):
        client = Client(lambda path, params: {"data": [{"name": "page_media_view", "period": "day", "total_value": {"value": 44}}]} if params.get("metric") == "page_media_view" else None)
        self.assertIsNone(self.fetch(client)["views"])

    def test_duplicate_series_in_one_response_are_rejected(self):
        duplicate = series("page_media_view")
        duplicate["data"].append(dict(duplicate["data"][0]))
        client = Client(lambda path, params: duplicate if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertIsNone(row["views"])
        self.assertEqual(row["metrics"]["views"]["data_points"], 0)

    def test_pagination_deduplicates_bucket_and_copies_only_cursor(self):
        first = series("page_media_view", (10,))
        first["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?after=second&metric=page_impressions&access_token=private-page-token&since=1"}
        second = series("page_media_view", (10, 20))
        responses = [first, second]
        client = Client(lambda path, params: responses.pop(0) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["data_points"], 2)
        reads = [call for call in client.calls if call["params"].get("metric") == "page_media_view"]
        self.assertEqual(reads[1]["path"], "123/insights")
        self.assertEqual(reads[1]["params"], {"metric": "page_media_view", "period": "day", "since": SINCE, "until": SINCE + 2 * DAY, "after": "second"})
        self.assert_safe(row)

    def test_pagination_retains_native_preset_without_conflicting_bounds(self):
        first = series("page_media_view", (10,))
        first["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?after=second&date_preset=last_30d&since=1&access_token=private-page-token"}
        responses = [first, series("page_media_view", (10, 20))]
        client = Client(lambda path, params: responses.pop(0) if params.get("metric") == "page_media_view" else None)
        fetch_page_analytics(client, PAGE, SINCE, SINCE + 7 * DAY, range_days=7, date_preset="last_7d")
        reads = [call for call in client.calls if call["params"].get("metric") == "page_media_view"]
        self.assertEqual(reads[1]["params"], {"metric": "page_media_view", "period": "day", "date_preset": "last_7d", "after": "second"})

    def test_cursor_links_are_rebuilt_for_configured_page_version_metric_and_range(self):
        for path in (
            '/v25.0/123/insights', '/123/insights', '/v24.0/123/insights/',
            '/v25.0/123/insights/page_media_view', '/123/insights/page_media_view/',
        ):
            with self.subTest(path=path):
                first = series('page_media_view', (10,))
                first['paging'] = {'next': 'https://graph.facebook.com' + path + '?after=second&access_token=private-page-token&date_preset=last_30d&since=1'}
                responses = [first, series('page_media_view', (10, 20))]
                client = Client(lambda path, params: responses.pop(0) if params.get('metric') == 'page_media_view' else None)
                row = self.fetch(client)
                self.assertEqual(row['metrics']['views']['status'], 'ok')
                self.assertEqual(row['views'], 30)
                reads = [call for call in client.calls if call['params'].get('metric') == 'page_media_view']
                self.assertEqual(len(reads), 2)
                self.assertEqual(reads[1]['path'], '123/insights')
                self.assertEqual(reads[1]['params'], {'metric': 'page_media_view', 'period': 'day', 'since': SINCE, 'until': SINCE + 2 * DAY, 'after': 'second'})
                self.assert_safe(row)

    def test_conflicting_duplicate_bucket_is_excluded_not_chosen_arbitrarily(self):
        first = series("page_media_view", (10,))
        first["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?after=second"}
        responses = [first, series("page_media_view", (99, 20))]
        client = Client(lambda path, params: responses.pop(0) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 20)
        self.assertEqual(row["metrics"]["views"]["data_points"], 1)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")

    def test_all_conflicting_buckets_become_unavailable_not_zero(self):
        data = series("page_media_view", (10,))
        data["data"][0]["values"].append({"value": 99, "end_time": data["data"][0]["values"][0]["end_time"]})
        client = Client(lambda path, params: data if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertIsNone(row["views"])
        self.assertEqual(row["metrics"]["views"]["status"], "unavailable")

    def test_pagination_rejects_other_hosts_pages_edges_metrics_and_credentials(self):
        for url in (
            "https://attacker.example/v24.0/123/insights?after=x&access_token=private-page-token",
            "https://graph.facebook.com/v24.0/456/insights?after=x",
            "https://graph.facebook.com/v24.0/123/feed?after=x",
            "https://graph.facebook.com/v24.0/123/insights/page_post_engagements?after=x",
            "https://graph.facebook.com/v24.0/123/insights/extra/path?after=x",
            "https://graph.facebook.com/v25.1/123/insights?after=x",
            "http://graph.facebook.com/v24.0/123/insights?after=x",
            "https://graph.facebook.com@attacker.example/v24.0/123/insights?after=x",
        ):
            with self.subTest(url=url):
                first = series("page_media_view", (10,))
                first["paging"] = {"next": url}
                client = Client(lambda path, params: first if params.get("metric") == "page_media_view" else None)
                row = self.fetch(client)
                self.assertEqual(row["views"], 10)
                self.assertEqual(row["metrics"]["views"]["status"], "partial")
                self.assertEqual(sum(call["params"].get("metric") == "page_media_view" for call in client.calls), 1)
                self.assert_safe(row)

    def test_blank_duplicate_and_repeated_query_cursors_are_not_followed(self):
        for query in ('after=', 'after=x&after=', 'after=x&after=y', 'after=x&after=x', 'after=x%0Ay', 'after=%00', 'after=x%7Fy'):
            with self.subTest(query=query):
                first = series('page_media_view')
                first['paging'] = {'next': 'https://graph.facebook.com/v24.0/123/insights?' + query}
                client = Client(lambda path, params: first if params.get('metric') == 'page_media_view' else None)
                row = self.fetch(client)
                self.assertEqual(row['views'], 30)
                self.assertEqual(row['metrics']['views']['status'], 'partial')
                self.assertEqual(sum(call['params'].get('metric') == 'page_media_view' for call in client.calls), 1)
                self.assert_safe(row)

    def test_pagination_detects_repeating_cursor(self):
        data = series("page_media_view")
        data["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?after=repeated"}
        client = Client(lambda path, params: data if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")
        self.assertEqual(sum(call["params"].get("metric") == "page_media_view" for call in client.calls), 2)

    def test_temporal_next_link_does_not_extend_selected_range(self):
        data = series("page_media_view")
        data["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?since=9999999999&until=99999999999&access_token=private-page-token"}
        client = Client(lambda path, params: data if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["status"], "ok")
        self.assertEqual(sum(call["params"].get("metric") == "page_media_view" for call in client.calls), 1)

    def test_complete_thirty_day_report_ignores_canonical_temporal_navigation(self):
        for prefix in ('/v25.0/123/insights/', '/123/insights/'):
            with self.subTest(prefix=prefix):
                def handler(path, params):
                    if path == '123':
                        return {'followers_count': 3}
                    source = params['metric']
                    payload = series(source, tuple(range(30)))
                    payload['paging'] = {'next': 'https://graph.facebook.com' + prefix + source + '?access_token=private-page-token&since=9999999999&until=99999999999'}
                    return payload
                client = Client(handler)
                row = fetch_page_analytics(client, PAGE, SINCE, SINCE + 30 * DAY, range_days=30, date_preset='last_30d')
                self.assertEqual(row['status'], 'ok')
                self.assertEqual(row['issues'], [])
                self.assertEqual(len(client.calls), 4)
                for key in ('views', 'engagements', 'new_followers'):
                    self.assertEqual(row[key], sum(range(30)))
                    self.assertEqual(row['metrics'][key]['data_points'], 30)
                    self.assertEqual(row['metrics'][key]['status'], 'ok')
                for call in client.calls[1:]:
                    self.assertEqual(call['path'], '123/insights')
                    self.assertEqual(call['params']['date_preset'], 'last_30d')
                    self.assertNotIn('since', call['params'])
                    self.assertNotIn('until', call['params'])
                self.assert_safe(row)

    def test_ignored_temporal_link_still_checks_https_and_meta_host(self):
        for url in (
            'https://attacker.example/v24.0/123/insights?since=1&access_token=private-page-token',
            'http://graph.facebook.com/v24.0/123/insights?since=1',
            'https://graph.facebook.com@attacker.example/v24.0/123/insights?since=1',
            'https://graph.facebook.com:444/v24.0/123/insights?since=1',
            'https://graph.facebook.com/v24.0/123/insights?since=1#fragment',
        ):
            with self.subTest(url=url):
                payload = series('page_media_view')
                payload['paging'] = {'next': url}
                client = Client(lambda path, params: payload if params.get('metric') == 'page_media_view' else None)
                row = self.fetch(client)
                self.assertEqual(row['metrics']['views']['status'], 'partial')
                self.assertEqual(sum(call['params'].get('metric') == 'page_media_view' for call in client.calls), 1)
                self.assert_safe(row)

    def test_api_bucket_end_time_is_not_trimmed_using_local_midnight(self):
        row = self.fetch(Client())
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["last_end_time"], "2026-10-03T07:00:00+00:00")
        self.assertGreater(datetime.fromisoformat(row["metrics"]["views"]["last_end_time"]).timestamp(), SINCE + 2 * DAY)

    def test_gaps_and_missing_days_are_partial(self):
        times = ("2026-10-02T07:00:00+00:00", "2026-10-04T07:00:00+00:00")
        client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
        self.assertEqual(self.fetch(client)["metrics"]["views"]["status"], "partial")
        client = Client(lambda path, params: series("page_media_view", (10,)) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 10)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")

    def test_daily_timezone_dst_shift_does_not_create_false_missing_day(self):
        for second in ("2026-10-03T06:00:00+00:00", "2026-10-03T08:00:00+00:00"):
            with self.subTest(second=second):
                times = ("2026-10-02T07:00:00+00:00", second)
                client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
                self.assertEqual(self.fetch(client)["metrics"]["views"]["status"], "ok")

    def test_overlapping_daily_buckets_are_excluded_not_added_to_totals(self):
        times = ("2026-10-02T07:00:00+00:00", "2026-10-02T08:00:00+00:00")
        client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertIsNone(row["views"])
        self.assertEqual(row["metrics"]["views"]["status"], "unavailable")
        self.assertEqual(row["metrics"]["views"]["data_points"], 0)
        self.assertTrue(any(issue["metric"] == "views" and "chồng lấn" in issue["message"] for issue in row["issues"]))

    def test_overlapping_days_do_not_hide_a_reliable_remaining_bucket(self):
        times = ("2026-10-02T07:00:00+00:00", "2026-10-02T08:00:00+00:00", "2026-10-03T08:00:00+00:00")
        client = Client(lambda path, params: series("page_media_view", (10, 20, 30), times) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")
        self.assertEqual(row["metrics"]["views"]["data_points"], 1)
        self.assertEqual(row["metrics"]["views"]["first_end_time"], times[2])

    def test_overlap_across_paginated_responses_is_excluded(self):
        first = series("page_media_view", (10,), ("2026-10-02T07:00:00+00:00",))
        first["paging"] = {"next": "https://graph.facebook.com/v24.0/123/insights?after=second"}
        second = series("page_media_view", (20,), ("2026-10-02T08:00:00+00:00",))
        responses = [first, second]
        client = Client(lambda path, params: responses.pop(0) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertIsNone(row["views"])
        self.assertEqual(row["metrics"]["views"]["data_points"], 0)
        self.assertEqual(sum(call["params"].get("metric") == "page_media_view" for call in client.calls), 2)

    def test_twenty_three_hour_dst_buckets_on_same_utc_date_are_distinct(self):
        times = ("2026-10-02T00:30:00+00:00", "2026-10-02T23:30:00+00:00")
        client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["status"], "ok")
        self.assertEqual(row["metrics"]["views"]["data_points"], 2)

    def test_twenty_six_hour_spacing_is_partial_not_complete_daily_coverage(self):
        times = ("2026-10-02T07:00:00+00:00", "2026-10-03T09:00:00+00:00")
        client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
        row = self.fetch(client)
        self.assertEqual(row["views"], 30)
        self.assertEqual(row["metrics"]["views"]["status"], "partial")

    def test_missing_timezone_or_missing_end_time_is_not_counted(self):
        for times in (("2026-10-02T07:00:00", "2026-10-03T07:00:00"), (None, "private-page-token")):
            with self.subTest(times=times):
                client = Client(lambda path, params: series("page_media_view", (10, 20), times) if params.get("metric") == "page_media_view" else None)
                row = self.fetch(client)
                self.assertIsNone(row["views"])
                self.assert_safe(row)

    def test_cancellation_propagates_without_more_reads(self):
        client = Client(lambda path, params: AnalyticsCancelled("cancelled") if params.get("metric") == "page_media_view" else None)
        with self.assertRaises(AnalyticsCancelled):
            self.fetch(client)
        self.assertEqual(len(client.calls), 2)
        client = Client()
        client.auth_error = AnalyticsCancelled("cancelled")
        with self.assertRaises(AnalyticsCancelled):
            self.fetch(client)
        self.assertEqual(client.calls, [])

    def test_invalid_ranges_and_preset_days_reject_before_api_reads(self):
        cases = (
            (0, SINCE, {}), (SINCE, SINCE, {}), (True, SINCE, {}),
            (SINCE, SINCE + 100 * DAY, {}), (SINCE, SINCE + DAY, {"range_days": 0}),
            (SINCE, SINCE + DAY, {"range_days": True}), (SINCE, SINCE + DAY, {"date_preset": "lifetime"}),
            (SINCE, SINCE + DAY, {"date_preset": []}),
            (SINCE, SINCE + DAY, {"date_preset": "last_7d", "range_days": 1}),
        )
        for since, until, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                client = Client()
                with self.assertRaises(ValueError):
                    fetch_page_analytics(client, PAGE, since, until, **kwargs)
                self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
