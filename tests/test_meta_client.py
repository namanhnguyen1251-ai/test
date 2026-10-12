import json
import tempfile
import unittest
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from meta_client import GraphClient, GraphError


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    """Consume files inside request(), before production closes the stream."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        call = {"method": method, "url": url, **kwargs}
        self.calls.append(call)
        data = kwargs.get("data")
        if hasattr(data, "read"):
            call["streaming"] = True
            chunks = []
            while True:
                chunk = data.read(1024)
                if not chunk:
                    break
                chunks.append(chunk)
            call["body"] = b"".join(chunks)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


class MetaClientTests(unittest.TestCase):
    PAGE = {"id": "123", "name": "Page test", "access_token": "page-secret"}
    USER_TOKEN = "user-secret"
    BASE = "https://graph.facebook.com/v24.0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.photo = self.media("anh.png", "image/png", b"photo-content")
        self.video = self.media("video.mp4", "video/mp4", b"video-content")

    def media(self, name, mime, content):
        path = self.root / name
        path.write_bytes(content)
        return {"path": str(path), "name": name, "content_type": mime}

    def client(self, *responses, **kwargs):
        self.session = Session(responses)
        return GraphClient(self.USER_TOKEN, "v24.0", session=self.session, processing_timeout=0, **kwargs)

    def assert_secure_calls(self):
        for call in self.session.calls:
            self.assertIs(call["verify"], True)
            self.assertIs(call["allow_redirects"], False)
            self.assertNotIn(self.USER_TOKEN, call["url"])
            self.assertNotIn(self.PAGE["access_token"], call["url"])
            self.assertNotIn("access_token", call.get("params") or {})
            self.assertIn("timeout", call)

    def test_pages_follow_cursor_without_copying_token_query(self):
        client = self.client(
            Response({"data": [dict(self.PAGE, tasks=["CREATE_CONTENT"])], "paging": {"next": self.BASE + "/me/accounts?after=cursor2&access_token=some-secret"}}),
            Response({"data": [dict(self.PAGE), {"id": "456", "name": "Second", "access_token": "second-secret", "tasks": ["MANAGE"]}]}),
        )
        pages = client.list_pages()
        self.assertEqual([page["id"] for page in pages], ["123", "456"])
        self.assertEqual(pages[0]["tasks"], ["CREATE_CONTENT"])
        self.assertEqual(self.session.calls[1]["params"]["after"], "cursor2")
        self.assertEqual(self.session.calls[1]["url"], self.BASE + "/me/accounts")
        self.assertEqual(self.session.calls[0]["headers"]["Authorization"], "Bearer user-secret")
        self.assert_secure_calls()

    def test_pagination_rejects_other_hosts_versions_and_paths(self):
        urls = [
            "https://attacker.example/v24.0/me/accounts?after=x",
            "http://graph.facebook.com/v24.0/me/accounts?after=x",
            "https://graph.facebook.com@attacker.example/v24.0/me/accounts?after=x",
            "https://graph.facebook.com:443/v24.0/me/accounts?after=x",
            "https://graph.facebook.com/v23.0/me/accounts?after=x",
            "https://graph.facebook.com/v24.0/123/feed?after=x",
        ]
        for url in urls:
            with self.subTest(url=url):
                client = self.client(Response({"data": [], "paging": {"next": url}}))
                with self.assertRaises(GraphError):
                    client.list_pages()
                self.assertEqual(len(self.session.calls), 1)

    def test_pages_canonical_numeric_user_pagination_collects_all_fifty_pages(self):
        first_batch = [{"id": str(1000 + index), "name": f"Page {index}", "access_token": f"token-{index}", "tasks": ["CREATE_CONTENT"]} for index in range(25)]
        second_batch = [{"id": str(1000 + index), "name": f"Page {index}", "access_token": f"token-{index}", "tasks": ["CREATE_CONTENT"]} for index in range(25, 50)]
        client = self.client(
            Response({"data": first_batch, "paging": {"next": self.BASE + "/123456789/accounts?after=second-batch&access_token=user-secret"}}),
            Response({"data": second_batch}),
        )
        pages = client.list_pages()
        self.assertEqual(len(pages), 50)
        self.assertEqual(len({page["id"] for page in pages}), 50)
        self.assertEqual(self.session.calls[1]["url"], self.BASE + "/me/accounts")
        self.assertEqual(self.session.calls[1]["params"]["after"], "second-batch")
        self.assert_secure_calls()

    def test_pagination_detects_repeated_cursor(self):
        response = Response({"data": [], "paging": {"next": self.BASE + "/me/accounts?after=loop"}})
        client = self.client(response, response)
        with self.assertRaises(GraphError):
            client.list_pages()
        self.assertEqual(len(self.session.calls), 2)

    def test_text_publishing_has_no_automatic_retry_or_token_in_body(self):
        client = self.client(Response({"id": "123_999"}))
        result = client.publish(self.PAGE, "text", "Xin chào", [])
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["id"], "123_999")
        self.assertEqual(self.session.calls[0]["data"], {"message": "Xin chào"})
        self.assertEqual(self.session.calls[0]["headers"]["Authorization"], "Bearer page-secret")
        self.assertEqual(len(self.session.calls), 1)
        self.assertNotIn("access_token", result)
        self.assert_secure_calls()

    def test_missing_published_id_is_uncertain(self):
        client = self.client(Response({"success": True}))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Xin chào", [])
        self.assertTrue(caught.exception.uncertain)

    def test_multiple_photos_are_staged_then_attached_to_one_feed_post(self):
        second = self.media("hai.jpg", "image/jpeg", b"second-photo")
        client = self.client(Response({"id": "501"}), Response({"id": "502"}), Response({"id": "123_999"}))
        result = client.publish(self.PAGE, "photos", "Ảnh mới", [self.photo, second])
        self.assertEqual(result["status"], "published")
        self.assertEqual([call["url"] for call in self.session.calls], [self.BASE + "/123/photos", self.BASE + "/123/photos", self.BASE + "/123/feed"])
        for call in self.session.calls[:2]:
            self.assertIsInstance(call["data"], MultipartEncoder)
            self.assertTrue(call["streaming"])
            self.assertIn(b'name="published"\r\n\r\nfalse', call["body"])
            self.assertNotIn(b"page-secret", call["body"])
        final = self.session.calls[-1]["data"]
        self.assertEqual(json.loads(final["attached_media"]), [{"media_fbid": "501"}, {"media_fbid": "502"}])
        self.assertEqual(final["message"], "Ảnh mới")
        self.assert_secure_calls()

    def test_photo_failure_does_not_send_final_feed(self):
        client = self.client(Response({"error": {"code": 100, "message": "secret details"}}, 400))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "photos", "", [self.photo])
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.session.calls), 1)

    def test_every_photo_is_validated_before_any_upload(self):
        client = self.client()
        with self.assertRaises(GraphError):
            client.publish(self.PAGE, "photos", "", [self.photo, dict(self.photo, path=str(self.root / "missing.png"))])
        self.assertEqual(self.session.calls, [])

    def test_video_streaming_and_explicit_published_confirmation(self):
        client = self.client(
            Response({"id": "700"}),
            Response({"published": True, "status": {"video_status": "ready"}, "permalink_url": "https://www.facebook.com/watch/700"}),
        )
        result = client.publish(self.PAGE, "video", "Video của tôi", [self.video], "Tiêu đề")
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["permalink_url"], "https://www.facebook.com/watch/700")
        call = self.session.calls[0]
        self.assertEqual(call["url"], "https://graph-video.facebook.com/v24.0/123/videos")
        self.assertIsInstance(call["data"], MultipartEncoder)
        self.assertIn(b"video-content", call["body"])
        self.assertIn(b'name="published"\r\n\r\ntrue', call["body"])
        self.assertIn("published", self.session.calls[1]["params"]["fields"])
        self.assertEqual(self.session.calls[1]["url"], self.BASE + "/700")
        self.assert_secure_calls()

    def test_video_ready_does_not_prove_published(self):
        client = self.client(Response({"id": "700"}), Response({"published": False, "status": {"video_status": "ready"}}))
        result = client.publish(self.PAGE, "video", "", [self.video])
        self.assertEqual(result, {"id": "700", "status": "processing"})

    def test_video_processing_failure_is_safe(self):
        client = self.client(Response({"id": "700"}), Response({"status": {"video_status": "error", "processing_phase": {"errors": [{"message": "page-secret"}]}}}))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "video", "", [self.video])
        self.assertNotIn("page-secret", str(caught.exception))

    def test_status_read_failure_does_not_republish_acknowledged_video(self):
        client = self.client(Response({"id": "700"}), requests.Timeout("token=page-secret"))
        result = client.publish(self.PAGE, "video", "", [self.video])
        self.assertEqual(result["status"], "processing")
        self.assertEqual([call["method"] for call in self.session.calls], ["POST", "GET"])

    def test_reel_start_stream_finish_and_published_status(self):
        upload_url = "https://rupload.facebook.com/video-upload/v24.0/800"
        client = self.client(
            Response({"video_id": "800", "upload_url": upload_url}),
            Response({"success": True}),
            Response({"status": {"uploading_phase": {"status": "complete"}, "video_status": "processing"}}),
            Response({"success": True}),
            Response({"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}}),
        )
        result = client.publish(self.PAGE, "reel", "Reel của tôi", [self.video], "Tiêu đề")
        self.assertEqual(result["status"], "published")
        raw = self.session.calls[1]
        self.assertEqual(raw["url"], upload_url)
        self.assertEqual(raw["headers"]["Authorization"], "OAuth page-secret")
        self.assertEqual(raw["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(raw["headers"]["offset"], "0")
        self.assertEqual(raw["headers"]["file_size"], str(len(b"video-content")))
        self.assertEqual(raw["body"], b"video-content")
        self.assertEqual(self.session.calls[3]["data"], {"upload_phase": "finish", "video_id": "800", "video_state": "PUBLISHED", "description": "Reel của tôi", "title": "Tiêu đề"})
        self.assert_secure_calls()

    def test_reel_ready_with_incomplete_publishing_stays_processing(self):
        client = self.client(
            Response({"video_id": "800"}), Response({"success": True}),
            Response({"status": {"uploading_phase": {"status": "complete"}}}),
            Response({"success": True}),
            Response({"status": {"video_status": "ready", "publishing_phase": {"status": "in_progress"}}}),
        )
        self.assertEqual(client.publish(self.PAGE, "reel", "", [self.video])["status"], "processing")

    def test_reel_upload_service_version_can_differ_from_graph_version(self):
        upload_url = "https://rupload.facebook.com/video-upload/v19.0/800"
        client = self.client(
            Response({"video_id": "800", "upload_url": upload_url}),
            Response({"success": True}),
            Response({"status": {"uploading_phase": {"status": "complete"}}}),
            Response({"success": True}),
            Response({"status": {"publishing_phase": {"status": "complete"}}}),
        )
        result = client.publish(self.PAGE, "reel", "", [self.video])
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.session.calls[1]["url"], upload_url)
        self.assertEqual(self.session.calls[0]["url"], self.BASE + "/123/video_reels")
        self.assertEqual(self.session.calls[3]["url"], self.BASE + "/123/video_reels")
        self.assert_secure_calls()

    def test_reel_provider_query_and_unversioned_path_are_preserved(self):
        queries = "?sig=opaque-upload-signature&hash=abc%2Fdef%2Bghi%3D&expires=1234567890"
        urls = [
            "https://rupload.facebook.com/video-upload/v24.0/800" + queries,
            "https://rupload.facebook.com/video-upload/v19.0/800" + queries,
            "https://rupload.facebook.com/video-upload/800" + queries,
            "https://rupload.facebook.com/video-upload/v19.0/800/" + queries,
        ]
        for upload_url in urls:
            with self.subTest(upload_url=upload_url):
                client = self.client(
                    Response({"video_id": "800", "upload_url": upload_url}),
                    Response({"success": True}),
                    Response({"status": {"uploading_phase": {"status": "complete"}}}),
                    Response({"success": True}),
                    Response({"status": {"publishing_phase": {"status": "complete"}}}),
                )
                result = client.publish(self.PAGE, "reel", "", [self.video])
                self.assertEqual(self.session.calls[1]["url"], upload_url)
                self.assertIsNone(self.session.calls[1]["params"])
                self.assertEqual(result["status"], "published")
                self.assertNotIn("opaque-upload-signature", json.dumps(result))
                self.assert_secure_calls()

    def test_reel_provider_query_does_not_leak_from_upload_error(self):
        upload_url = "https://rupload.facebook.com/video-upload/v19.0/800?sig=opaque-upload-signature"
        client = self.client(Response({"video_id": "800", "upload_url": upload_url}), requests.Timeout(upload_url + " page-secret"))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "reel", "", [self.video])
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn("opaque-upload-signature", str(caught.exception))
        self.assertNotIn("page-secret", str(caught.exception))
        self.assertEqual(len(self.session.calls), 2)

    def test_reel_upload_redirect_is_not_followed(self):
        upload_url = "https://rupload.facebook.com/video-upload/v19.0/800?sig=opaque-upload-signature"
        client = self.client(Response({"video_id": "800", "upload_url": upload_url}), Response({}, status_code=302))
        with self.assertRaises(GraphError):
            client.publish(self.PAGE, "reel", "", [self.video])
        self.assertEqual(len(self.session.calls), 2)
        self.assert_secure_calls()

    def test_reel_rejects_untrusted_upload_url_before_sending_file_or_token(self):
        urls = [
            "https://attacker.example/upload", "http://rupload.facebook.com/upload",
            "https://rupload.facebook.com.attacker.example/upload",
            "https://rupload.facebook.com:443/upload", "https://rupload.facebook.com/upload?access_token=page-secret",
            "https://rupload.facebook.com/video-upload/v24.0/other",
            "https://rupload.facebook.com/video-upload/v24.0/801?sig=opaque-upload-signature",
            "https://rupload.facebook.com/video-upload/v24.0/800?sig=x#fragment",
            "https://rupload.facebook.com/video-upload/v24.0/800#",
            "https://rupload.facebook.com/video-upload/v24.0/800/extra",
            "https://rupload.facebook.com/video-upload/v24.0/800/../801",
            "https://user:password@rupload.facebook.com/video-upload/v24.0/800",
            "https://rupload.facebook.com\\@attacker.example/video-upload/v24.0/800",
            "https://rupload.face\nbook.com/video-upload/v24.0/800",
            "\x00https://rupload.facebook.com/video-upload/v24.0/800",
            " https://rupload.facebook.com/video-upload/v24.0/800",
            "https://rupload.facebook.com/video-upload/v24.0/800?sig=x\r\n",
            "https://rupload.facebook.com/video-upload/v24.0/800?sig=x\x7f",
        ]
        for url in urls:
            with self.subTest(url=url):
                client = self.client(Response({"video_id": "800", "upload_url": url}))
                with self.assertRaises(GraphError):
                    client.publish(self.PAGE, "reel", "", [self.video])
                self.assertEqual(len(self.session.calls), 1)

    def test_reel_still_uploading_never_sends_publish(self):
        client = self.client(
            Response({"video_id": "800"}), Response({"success": True}),
            Response({"status": {"uploading_phase": {"status": "in_progress"}}}),
        )
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "reel", "", [self.video])
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.session.calls), 3)

    def test_reel_false_upload_acknowledgement_never_sends_publish(self):
        client = self.client(Response({"video_id": "800"}), Response({"success": False}))
        with self.assertRaises(GraphError):
            client.publish(self.PAGE, "reel", "", [self.video])
        self.assertEqual(len(self.session.calls), 2)

    def test_post_timeout_is_uncertain_and_is_not_retried(self):
        client = self.client(requests.Timeout("https://graph.facebook.com/?access_token=page-secret"))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn("page-secret", str(caught.exception))
        self.assertEqual(len(self.session.calls), 1)

    def test_read_timeout_is_certainly_not_a_publish(self):
        client = self.client(requests.Timeout("user-secret"))
        with self.assertRaises(GraphError) as caught:
            client.list_pages()
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn("user-secret", str(caught.exception))

    def test_meta_error_messages_are_redacted_and_permission_code_survives(self):
        client = self.client(Response({"error": {"code": 200, "message": "Oops user-secret page-secret access_token=something"}}, 403))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertEqual(caught.exception.code, 200)
        self.assertFalse(caught.exception.uncertain)
        for secret in ["user-secret", "page-secret", "access_token", "something"]:
            self.assertNotIn(secret, str(caught.exception))

    def test_rate_limit_is_not_retried(self):
        client = self.client(Response({"error": {"code": 613}}, 429))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertEqual(caught.exception.code, 613)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.session.calls), 1)

    def test_server_failure_after_publish_is_uncertain(self):
        client = self.client(Response({"error": {"code": 2, "message": "page-secret"}}, 500))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertTrue(caught.exception.uncertain)

    def test_internal_error_code_with_http_200_still_marks_publish_uncertain(self):
        client = self.client(Response({"error": {"code": 2, "message": "page-secret"}}))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertTrue(caught.exception.uncertain)

    def test_post_redirect_does_not_forward_credentials(self):
        client = self.client(Response({}, 307))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.session.calls), 1)
        self.assert_secure_calls()

    def test_invalid_json_after_post_is_uncertain(self):
        client = self.client(Response(ValueError("page-secret")))
        with self.assertRaises(GraphError) as caught:
            client.publish(self.PAGE, "text", "Hello", [])
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("page-secret", str(caught.exception))

    def test_malformed_or_secret_permalink_is_ignored(self):
        for permalink in ["https://[invalid", "https://www.facebook.com/page-secret", "https://attacker.example/post"]:
            with self.subTest(permalink=permalink):
                client = self.client(Response({"id": "123_999", "permalink_url": permalink}))
                result = client.publish(self.PAGE, "text", "Hello", [])
                self.assertEqual(result["permalink_url"], "https://www.facebook.com/123_999")

    def test_validates_version_and_token_without_network(self):
        for version in ["", "24", "v24", "v24.1", "v24.0/123/feed", "https://evil.example"]:
            with self.subTest(version=version), self.assertRaises(GraphError):
                GraphClient(self.USER_TOKEN, version)
        with self.assertRaises(GraphError):
            GraphClient("bad\r\nAuthorization: secret", "v24.0")

    def test_default_session_explicitly_disables_retries(self):
        client = GraphClient(self.USER_TOKEN, "v24.0")
        self.addCleanup(client.close)
        self.assertEqual(client.session.get_adapter(client.base_url).max_retries.total, 0)


if __name__ == "__main__":
    unittest.main()
