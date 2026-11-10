"""Unit tests for webhook allowlist + debounce (mocked HTTP)."""

from __future__ import annotations

import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ratspeak_mcp.webhook import (
    DEFAULT_WEBHOOK_ALLOWLIST,
    already_fired,
    mark_fired,
    maybe_fire_inbound_webhook,
    parse_allowlist,
)


class ParseAllowlistTests(unittest.TestCase):
    def test_default_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RATSPEAK_WEBHOOK_ALLOWLIST", None)
            allow = parse_allowlist(None)
        self.assertEqual(allow, {DEFAULT_WEBHOOK_ALLOWLIST})

    def test_empty_string_uses_default(self) -> None:
        self.assertEqual(parse_allowlist(""), {DEFAULT_WEBHOOK_ALLOWLIST})
        self.assertEqual(parse_allowlist("   "), {DEFAULT_WEBHOOK_ALLOWLIST})

    def test_comma_separated(self) -> None:
        allow = parse_allowlist("AaBb, 60c8a4197d1de58a53f3a95a1c97f4af ,deadbeef")
        self.assertEqual(
            allow,
            {"aabb", "60c8a4197d1de58a53f3a95a1c97f4af", "deadbeef"},
        )


class DebounceAndFireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.peer = DEFAULT_WEBHOOK_ALLOWLIST
        self.url = "https://example.test/wake"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_mark_and_already_fired(self) -> None:
        self.assertFalse(already_fired(self.home, "msg-1"))
        mark_fired(self.home, "msg-1")
        self.assertTrue(already_fired(self.home, "msg-1"))
        raw = json.loads((self.home / "webhook_fired.json").read_text(encoding="utf-8"))
        self.assertIn("msg-1", raw["message_ids"])

    def test_no_url_skips_post(self) -> None:
        posts: list = []

        def fake_post(url, payload, bearer=None, timeout=10.0):
            posts.append((url, payload, bearer))

        with mock.patch.dict(os.environ, {"RATSPEAK_WEBHOOK_URL": ""}, clear=False):
            ok = maybe_fire_inbound_webhook(
                home=self.home,
                message_id="m1",
                source_hash=self.peer,
                destination_hash="dest",
                title="t",
                content="hello",
                timestamp=1.0,
                url="",  # explicit unset
                post_fn=fake_post,
            )
        self.assertFalse(ok)
        self.assertEqual(posts, [])

    def test_allowlist_blocks_unknown_source(self) -> None:
        posts: list = []

        def fake_post(url, payload, bearer=None, timeout=10.0):
            posts.append(payload)

        ok = maybe_fire_inbound_webhook(
            home=self.home,
            message_id="m2",
            source_hash="ffffffffffffffffffffffffffffffff",
            destination_hash="dest",
            title="t",
            content="hello",
            timestamp=1.0,
            url=self.url,
            allowlist={self.peer},
            post_fn=fake_post,
        )
        self.assertFalse(ok)
        self.assertEqual(posts, [])

    def test_fires_and_debounces(self) -> None:
        posts: list = []

        def fake_post(url, payload, bearer=None, timeout=10.0):
            posts.append({"url": url, "payload": payload, "bearer": bearer})

        common = dict(
            home=self.home,
            source_hash=self.peer,
            destination_hash="abcd" * 8,
            title="hi",
            content="x" * 250,
            timestamp=42.0,
            url=self.url,
            allowlist={self.peer},
            bearer="secret-key",
            post_fn=fake_post,
        )
        ok1 = maybe_fire_inbound_webhook(message_id="m3", **common)
        ok2 = maybe_fire_inbound_webhook(message_id="m3", **common)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["url"], self.url)
        self.assertEqual(p["bearer"], "secret-key")
        self.assertEqual(p["payload"]["event"], "ratspeak.inbound")
        self.assertEqual(p["payload"]["message_id"], "m3")
        self.assertEqual(p["payload"]["source_hash"], self.peer)
        self.assertEqual(len(p["payload"]["content_preview"]), 200)
        self.assertTrue(already_fired(self.home, "m3"))

    def test_http_error_soft_fails_no_mark(self) -> None:
        def boom(url, payload, bearer=None, timeout=10.0):
            raise RuntimeError("network down")

        ok = maybe_fire_inbound_webhook(
            home=self.home,
            message_id="m4",
            source_hash=self.peer,
            destination_hash="dest",
            title="t",
            content="body",
            timestamp=1.0,
            url=self.url,
            allowlist={self.peer},
            post_fn=boom,
        )
        self.assertFalse(ok)
        self.assertFalse(already_fired(self.home, "m4"))


if __name__ == "__main__":
    unittest.main()


class QueueAndRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.peer = DEFAULT_WEBHOOK_ALLOWLIST
        self.url = "https://example.test/wake"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _fire(self, message_id: str, post_fn, **extra):
        kwargs = dict(
            home=self.home,
            message_id=message_id,
            source_hash=self.peer,
            destination_hash="dest",
            title="t",
            content="body",
            timestamp=1.0,
            url=self.url,
            allowlist={self.peer},
            post_fn=post_fn,
        )
        kwargs.update(extra)
        return maybe_fire_inbound_webhook(**kwargs)

    def test_http_error_queues_for_retry(self) -> None:
        from ratspeak_mcp.webhook import pending_count, queue_path

        def boom(url, payload, bearer=None, timeout=10.0):
            raise RuntimeError("network down")

        ok = self._fire("m-queue", boom)
        self.assertFalse(ok)
        self.assertFalse(already_fired(self.home, "m-queue"))
        self.assertEqual(pending_count(self.home), 1)
        raw = json.loads(queue_path(self.home).read_text(encoding="utf-8"))
        item = raw["items"][0]
        self.assertEqual(item["message_id"], "m-queue")
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["last_error"], "RuntimeError")
        self.assertGreater(item["next_attempt_at"], 0)

    def test_retry_succeeds_and_drains_queue(self) -> None:
        from ratspeak_mcp.webhook import pending_count, retry_pending_webhooks

        calls = {"n": 0}

        def flaky(url, payload, bearer=None, timeout=10.0):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("not yet")
            return 200

        self.assertFalse(self._fire("m-retry", flaky))
        self.assertEqual(pending_count(self.home), 1)

        # Backoff would skip if we honor next_attempt_at; force it.
        result = retry_pending_webhooks(
            self.home, url=self.url, post_fn=flaky, force=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertTrue(already_fired(self.home, "m-retry"))
        self.assertEqual(calls["n"], 2)

    def test_backoff_schedule(self) -> None:
        from ratspeak_mcp.webhook import backoff_seconds

        self.assertEqual(backoff_seconds(0), 0.0)
        self.assertEqual(backoff_seconds(1), 1.0)
        self.assertEqual(backoff_seconds(2), 2.0)
        self.assertEqual(backoff_seconds(3), 4.0)
        self.assertEqual(backoff_seconds(20), 900.0)

    def test_due_retry_without_force(self) -> None:
        from ratspeak_mcp.webhook import retry_pending_webhooks

        def boom(url, payload, bearer=None, timeout=10.0):
            raise RuntimeError("down")

        now = 1_000.0
        self.assertFalse(self._fire("m-due", boom))

        posts: list = []

        def ok_post(url, payload, bearer=None, timeout=10.0):
            posts.append(payload["message_id"])
            return 200

        # Too early: next_attempt_at is now+1 after first failure, but we don't
        # control now in maybe_fire. Use a far-past now for due, far-future for skip.
        result_skip = retry_pending_webhooks(
            self.home, now=0.0, url=self.url, post_fn=ok_post, force=False
        )
        self.assertEqual(result_skip["delivered"], 0)
        self.assertEqual(posts, [])

        result_due = retry_pending_webhooks(
            self.home, now=time.time() + 10_000, url=self.url, post_fn=ok_post, force=False
        )
        self.assertEqual(result_due["delivered"], 1)
        self.assertEqual(posts, ["m-due"])
        self.assertTrue(already_fired(self.home, "m-due"))

    def test_does_not_mark_fired_until_success(self) -> None:
        from ratspeak_mcp.webhook import pending_count

        def boom(url, payload, bearer=None, timeout=10.0):
            raise RuntimeError("down")

        self._fire("m-no-mark", boom)
        self._fire("m-no-mark", boom)  # already queued, still not fired
        self.assertFalse(already_fired(self.home, "m-no-mark"))
        self.assertEqual(pending_count(self.home), 1)

    def test_logs_attempt_without_secret(self) -> None:
        def boom(url, payload, bearer=None, timeout=10.0):
            raise RuntimeError("down")

        with self.assertLogs("ratspeak-bridge", level="WARNING") as cm:
            self._fire("m-log", boom, bearer="super-secret-token")
        joined = "\n".join(cm.output)
        self.assertIn("m-log", joined)
        self.assertIn("fail", joined)
        self.assertNotIn("super-secret-token", joined)
        self.assertNotIn(self.url, joined)



class LocalHttpRetryTests(unittest.TestCase):
    """End-to-end POST against a local HTTP server (no external network)."""

    def setUp(self) -> None:
        import http.server
        import threading
        from functools import partial

        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.peer = DEFAULT_WEBHOOK_ALLOWLIST
        self.hits: list[int] = []
        self.bodies: list[bytes] = []
        self._fail_first = 2

        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                test.bodies.append(body)
                n = len(test.hits) + 1
                test.hits.append(self.command == "POST")
                if n <= test._fail_first:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"no")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args):
                return

        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/wake"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._tmpdir.cleanup()

    def test_retries_until_2xx(self) -> None:
        from ratspeak_mcp.webhook import pending_count, retry_pending_webhooks, post_webhook

        ok = maybe_fire_inbound_webhook(
            home=self.home,
            message_id="local-1",
            source_hash=self.peer,
            destination_hash="dest",
            title="t",
            content="hello local",
            timestamp=1.0,
            url=self.url,
            allowlist={self.peer},
            bearer=None,
            post_fn=post_webhook,
        )
        self.assertFalse(ok)
        self.assertEqual(len(self.hits), 1)
        self.assertFalse(already_fired(self.home, "local-1"))
        self.assertEqual(pending_count(self.home), 1)

        # Force retries: first remaining failure (2nd overall) still 503, then 200.
        result = retry_pending_webhooks(self.home, url=self.url, force=True)
        # One force pass is a single attempt; may still be 503.
        if not already_fired(self.home, "local-1"):
            result = retry_pending_webhooks(self.home, url=self.url, force=True)
        self.assertTrue(already_fired(self.home, "local-1"))
        self.assertEqual(pending_count(self.home), 0)
        self.assertGreaterEqual(len(self.hits), 3)
        payload = json.loads(self.bodies[-1].decode("utf-8"))
        self.assertEqual(payload["message_id"], "local-1")
        self.assertEqual(payload["event"], "ratspeak.inbound")
