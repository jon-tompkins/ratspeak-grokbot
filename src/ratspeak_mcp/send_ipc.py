"""Local Unix-domain-socket IPC for outbound LXMF send via the always-on daemon.

Newline-delimited JSON over `$RATSPEAK_MCP_HOME/send.sock` (mode 0600).
Private keys are never logged or returned.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from ratspeak_mcp.bridge import data_home

SOCK_NAME = "send.sock"
DEFAULT_TIMEOUT = 120.0
LOG = logging.getLogger("ratspeak-bridge.send_ipc")

SendFn = Callable[..., dict[str, Any]]
StatusFn = Callable[[], dict[str, Any]]


def send_sock_path(home: Path | None = None) -> Path:
    root = Path(home) if home is not None else data_home()
    return root / SOCK_NAME


def ipc_available(home: Path | None = None) -> bool:
    path = send_sock_path(home)
    return path.exists() and path.is_socket()


def _error_response(error: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error}
    out.update(extra)
    return out


def handle_request(
    req: dict[str, Any],
    *,
    send_fn: SendFn | None = None,
    status_fn: StatusFn | None = None,
    webhook_retry_fn: Callable[[], dict[str, Any]] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Parse and dispatch one IPC request dict. Pure enough for unit tests."""
    if not isinstance(req, dict):
        return _error_response("request must be a JSON object")

    op = str(req.get("op") or "").strip().lower()
    if not op:
        return _error_response("missing op")

    if op == "ping":
        return {"ok": True, "pong": True}

    if op == "status":
        if status_fn is None:
            return _error_response("status not available")
        try:
            st = status_fn()
            # Public fields only — never private keys.
            out = {
                "ok": True,
                "started": st.get("started"),
                "destination_hash": st.get("destination_hash"),
                "tcp_online": st.get("tcp_online"),
                "inbox_count": st.get("inbox_count"),
                "display_name": st.get("display_name"),
                "data_home": st.get("data_home"),
            }
            if home is not None:
                from ratspeak_mcp.webhook import queue_stats

                out.update(queue_stats(home))
            return out
        except Exception as e:
            return _error_response(str(e))

    if op == "send":
        if send_fn is None:
            return _error_response("send not available")
        dest_hash = req.get("dest_hash")
        body = req.get("body")
        if not isinstance(dest_hash, str) or not dest_hash.strip():
            return _error_response("dest_hash required (32 hex)")
        if not isinstance(body, str):
            return _error_response("body required (string)")
        subject = req.get("subject")
        if subject is not None and not isinstance(subject, str):
            return _error_response("subject must be a string when present")
        try:
            result = send_fn(
                dest_hash=dest_hash.strip(),
                body=body,
                subject=(subject if subject else None),
            )
            # Ensure expected keys; bridge.send already returns them.
            if not isinstance(result, dict):
                return _error_response("send returned non-dict")
            out = dict(result)
            out.setdefault("ok", bool(out.get("delivered")))
            return out
        except Exception as e:
            return _error_response(
                str(e),
                dest_hash=dest_hash.strip().lower().replace(":", ""),
                subject=subject or "",
            )

    if op == "webhook_retry":
        if webhook_retry_fn is None:
            return _error_response("webhook_retry not available")
        try:
            result = webhook_retry_fn()
            if not isinstance(result, dict):
                return _error_response("webhook_retry returned non-dict")
            out = dict(result)
            out.setdefault("ok", True)
            return out
        except Exception as e:
            return _error_response(str(e))

    return _error_response(f"unknown op: {op}")


def parse_line(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        raise ValueError("empty request line")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    return data


def encode_response(resp: dict[str, Any]) -> bytes:
    return (json.dumps(resp, default=str) + "\n").encode("utf-8")


class SendIpcServer:
    """Background thread accepting NDJSON requests on a Unix domain socket."""

    def __init__(
        self,
        bridge: Any,
        home: Path | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bridge = bridge
        self._home = Path(home) if home is not None else data_home()
        self._path = send_sock_path(self._home)
        self._log = logger or LOG
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        self._home.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError as e:
                self._log.warning("could not remove stale socket %s: %s", self._path, e)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self._path))
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        srv.listen(8)
        srv.settimeout(1.0)
        self._sock = srv
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve_loop,
            name="ratspeak-send-ipc",
            daemon=True,
        )
        self._thread.start()
        self._log.info("send IPC listening on %s", self._path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError as e:
                self._log.warning("could not remove socket on shutdown: %s", e)
        self._log.info("send IPC stopped")

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                self._handle_conn(conn)
            except Exception as e:
                self._log.warning("IPC connection error: %s", e)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(DEFAULT_TIMEOUT)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1_000_000:
                conn.sendall(encode_response(_error_response("request too large")))
                return
        if not buf:
            return
        line, _, _rest = buf.partition(b"\n")
        try:
            req = parse_line(line)
        except Exception as e:
            conn.sendall(encode_response(_error_response(f"bad request: {e}")))
            return

        # Do not log private keys or full message bodies at INFO.
        op = str(req.get("op") or "")
        if op == "send":
            dest = str(req.get("dest_hash") or "")[:32]
            self._log.info("IPC send dest_hash=%s body_len=%s", dest, len(str(req.get("body") or "")))
        else:
            self._log.info("IPC op=%s", op)

        def _webhook_retry() -> dict[str, Any]:
            from ratspeak_mcp.webhook import retry_pending_webhooks

            return retry_pending_webhooks(self._home, logger=self._log)

        resp = handle_request(
            req,
            send_fn=self._bridge.send,
            status_fn=self._bridge.status,
            webhook_retry_fn=_webhook_retry,
            home=self._home,
        )
        conn.sendall(encode_response(resp))


def ipc_call(
    request: dict[str, Any],
    *,
    home: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send one NDJSON request to the daemon IPC socket; return response dict."""
    path = send_sock_path(home)
    if not path.exists():
        raise FileNotFoundError(f"send IPC socket not found: {path}")

    payload = (json.dumps(request, default=str) + "\n").encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 2_000_000:
                raise RuntimeError("IPC response too large")
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if not buf:
        raise RuntimeError("empty IPC response")
    line, _, _ = buf.partition(b"\n")
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("IPC response must be a JSON object")
    return data


def send_via_ipc(
    dest_hash: str,
    body: str,
    subject: str | None = None,
    *,
    home: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    req: dict[str, Any] = {"op": "send", "dest_hash": dest_hash, "body": body}
    if subject:
        req["subject"] = subject
    return ipc_call(req, home=home, timeout=timeout)
