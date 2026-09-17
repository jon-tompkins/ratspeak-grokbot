"""Unit tests for send IPC protocol parsing/handler (mocked bridge send)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ratspeak_mcp.send_ipc import (
    SendIpcServer,
    encode_response,
    handle_request,
    ipc_available,
    ipc_call,
    parse_line,
    send_sock_path,
)


class ParseLineTests(unittest.TestCase):
    def test_parse_object(self) -> None:
        req = parse_line('{"op":"ping"}\n')
        self.assertEqual(req["op"], "ping")

    def test_parse_bytes(self) -> None:
        req = parse_line(b'{"op":"status"}')
        self.assertEqual(req["op"], "status")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_line("   ")

    def test_non_object_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_line("[1,2]")


class HandleRequestTests(unittest.TestCase):
    def test_ping(self) -> None:
        resp = handle_request({"op": "ping"})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["pong"])

    def test_missing_op(self) -> None:
        resp = handle_request({})
        self.assertFalse(resp["ok"])
        self.assertIn("op", resp["error"])

    def test_unknown_op(self) -> None:
        resp = handle_request({"op": "explode"})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown", resp["error"])

    def test_status(self) -> None:
        def status_fn():
            return {
                "started": True,
                "destination_hash": "abcd" * 8,
                "tcp_online": True,
                "inbox_count": 3,
                "display_name": "Rat",
                "data_home": "/tmp/x",
                "private_key_should_not_leak": "SECRET",
            }

        resp = handle_request({"op": "status"}, status_fn=status_fn)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["destination_hash"], "abcd" * 8)
        self.assertNotIn("private_key_should_not_leak", resp)

    def test_send_success_mirrors_bridge_keys(self) -> None:
        calls: list[dict] = []

        def send_fn(*, dest_hash, body, subject=None):
            calls.append({"dest_hash": dest_hash, "body": body, "subject": subject})
            return {
                "ok": True,
                "message_id": "mid-1",
                "dest_hash": dest_hash.lower(),
                "subject": subject or "",
                "method": "DIRECT",
                "lxmf_state": 8,
                "delivered": True,
            }

        resp = handle_request(
            {
                "op": "send",
                "dest_hash": "60c8a4197d1de58a53f3a95a1c97f4af",
                "body": "hello",
                "subject": "subj",
            },
            send_fn=send_fn,
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["message_id"], "mid-1")
        self.assertEqual(resp["dest_hash"], "60c8a4197d1de58a53f3a95a1c97f4af")
        self.assertEqual(resp["subject"], "subj")
        self.assertEqual(resp["method"], "DIRECT")
        self.assertEqual(resp["lxmf_state"], 8)
        self.assertTrue(resp["delivered"])
        self.assertEqual(calls[0]["body"], "hello")
        self.assertEqual(calls[0]["subject"], "subj")

    def test_send_missing_dest(self) -> None:
        resp = handle_request({"op": "send", "body": "x"}, send_fn=lambda **k: {})
        self.assertFalse(resp["ok"])
        self.assertIn("dest_hash", resp["error"])

    def test_send_missing_body(self) -> None:
        resp = handle_request(
            {"op": "send", "dest_hash": "a" * 32},
            send_fn=lambda **k: {},
        )
        self.assertFalse(resp["ok"])
        self.assertIn("body", resp["error"])

    def test_send_bridge_exception(self) -> None:
        def boom(*, dest_hash, body, subject=None):
            raise TimeoutError("No path to peer")

        resp = handle_request(
            {"op": "send", "dest_hash": "b" * 32, "body": "hi"},
            send_fn=boom,
        )
        self.assertFalse(resp["ok"])
        self.assertIn("No path", resp["error"])
        self.assertNotIn("private", json.dumps(resp).lower())

    def test_encode_response_ndjson(self) -> None:
        raw = encode_response({"ok": True})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), {"ok": True})


class SendIpcServerIntegrationTests(unittest.TestCase):
    """Socket round-trip with mocked bridge — no live mesh."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_server_ping_and_send(self) -> None:
        class FakeBridge:
            def send(self, *, dest_hash, body, subject=None):
                return {
                    "ok": True,
                    "message_id": "out-abc",
                    "dest_hash": dest_hash,
                    "subject": subject or "",
                    "method": "DIRECT",
                    "lxmf_state": 8,
                    "delivered": True,
                }

            def status(self):
                return {
                    "started": True,
                    "destination_hash": "979445b892d4476188ae2ef688fae578",
                    "tcp_online": True,
                    "inbox_count": 0,
                    "display_name": "Rat",
                    "data_home": "/tmp/fake",
                }

        bridge = FakeBridge()
        srv = SendIpcServer(bridge, home=self.home)
        srv.start()
        try:
            self.assertTrue(ipc_available(self.home))
            mode = send_sock_path(self.home).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

            ping = ipc_call({"op": "ping"}, home=self.home, timeout=5.0)
            self.assertTrue(ping.get("pong"))

            st = ipc_call({"op": "status"}, home=self.home, timeout=5.0)
            self.assertTrue(st["ok"])
            self.assertEqual(st["destination_hash"], "979445b892d4476188ae2ef688fae578")

            send = ipc_call(
                {
                    "op": "send",
                    "dest_hash": "60c8a4197d1de58a53f3a95a1c97f4af",
                    "body": "test body",
                    "subject": "",
                },
                home=self.home,
                timeout=5.0,
            )
            self.assertTrue(send["ok"])
            self.assertEqual(send["message_id"], "out-abc")
            self.assertTrue(send["delivered"])
        finally:
            srv.stop()

        self.assertFalse(ipc_available(self.home))


if __name__ == "__main__":
    unittest.main()
