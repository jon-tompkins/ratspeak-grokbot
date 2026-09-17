"""Unit tests for webhook allowlist + debounce (mocked HTTP)."""

from __future__ import annotations

import json
import os
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
