#!/usr/bin/env python3
"""Zero-inference health check for ratspeak-bridge.

Checks process, send.sock, and IPC ping/status. If unhealthy, performs a
clean restart (pkill, wait, rm send.sock, start-bridge.sh). Also retries
any queued webhook deliveries.

Does not send LXMF messages to the owner (or to anyone). End-to-end mesh
loopback is not used by default; health is IPC + process + socket. See
--help and the README.

This script is a looping process (no cron on the Grok Bot box). It does
not survive a box restart on its own: start-bridge.sh launches it, so it
comes back when the bridge start script is run after a restart.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_HOME = Path("/home/box/.ratspeak-mcp")
DEFAULT_START = Path("/home/box/.ratspeak-mcp/start-bridge.sh")
DEFAULT_LOG = Path("/tmp/ratspeak-watchdog.out")
PROCESS_PATTERN = "/ratspeak-bridge"
# Consecutive failed checks before restart (a single IPC blip should not
# bounce the daemon).
FAIL_THRESHOLD = 2
# Do not restart more often than this.
RESTART_COOLDOWN_S = 600
IPC_TIMEOUT_S = 8.0
PKILL_WAIT_S = 20.0


def _home() -> Path:
    env = os.environ.get("RATSPEAK_MCP_HOME")
    return Path(env).expanduser().resolve() if env else DEFAULT_HOME


def _log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)


def _state_path(home: Path) -> Path:
    return home / "watchdog-state.json"


def _load_state(home: Path) -> dict:
    path = _state_path(home)
    if not path.exists():
        return {"consecutive_failures": 0, "last_restart_at": 0, "last_ok_at": 0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"consecutive_failures": 0, "last_restart_at": 0, "last_ok_at": 0}


def _save_state(home: Path, state: dict) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _pgrep_bridge() -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", PROCESS_PATTERN],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            # Exclude this watchdog (pattern is /ratspeak-bridge, not watchdog).
            pids.append(pid)
    return pids


def _ipc_check(home: Path) -> tuple[bool, str, dict]:
    sock = home / "send.sock"
    if not sock.exists() or not sock.is_socket():
        return False, "send.sock missing or not a socket", {}
    try:
        # Import lazily so --help works without the venv path issues being confusing.
        from ratspeak_mcp.send_ipc import ipc_call

        ping = ipc_call({"op": "ping"}, home=home, timeout=IPC_TIMEOUT_S)
        if not ping.get("ok") or not ping.get("pong"):
            return False, f"ping failed: {ping}", ping
        status = ipc_call({"op": "status"}, home=home, timeout=IPC_TIMEOUT_S)
        if not status.get("ok"):
            return False, f"status failed: {status.get('error')}", status
        return True, "ok", status
    except Exception as exc:
        return False, f"ipc error {type(exc).__name__}: {exc}", {}


def _retry_webhooks(home: Path) -> dict:
    try:
        from ratspeak_mcp.webhook import retry_pending_webhooks

        result = retry_pending_webhooks(home)
        if result.get("delivered") or result.get("failed") or result.get("pending"):
            _log(
                "webhook retry delivered={0} failed={1} pending={2}".format(
                    result.get("delivered"),
                    result.get("failed"),
                    result.get("pending"),
                )
            )
        return result
    except Exception as exc:
        _log(f"webhook retry error: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def _clean_restart(home: Path, start_script: Path, bridge_log: Path) -> bool:
    _log("clean restart: pkill ratspeak-bridge")
    subprocess.run(["pkill", "-f", PROCESS_PATTERN], check=False)
    deadline = time.time() + PKILL_WAIT_S
    while time.time() < deadline:
        leftover = _pgrep_bridge()
        if not leftover:
            break
        time.sleep(0.2)
    leftover = _pgrep_bridge()
    if leftover:
        _log(f"pkill did not finish, sending SIGKILL to {leftover}")
        subprocess.run(["pkill", "-9", "-f", PROCESS_PATTERN], check=False)
        time.sleep(0.5)
        leftover = _pgrep_bridge()
        if leftover:
            _log(f"restart abort: still running {leftover}")
            return False

    sock = home / "send.sock"
    try:
        if sock.exists():
            sock.unlink()
            _log("removed stale send.sock")
    except OSError as exc:
        _log(f"rm send.sock failed: {exc}")
        return False

    if not start_script.is_file():
        _log(f"start script missing: {start_script}")
        return False

    log_fh = open(bridge_log, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            ["bash", str(start_script)],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    _log(f"started {start_script} pid={proc.pid}")

    # Wait until IPC comes up.
    deadline = time.time() + 30.0
    while time.time() < deadline:
        ok, reason, _st = _ipc_check(home)
        if ok:
            _log("restart healthy: ipc ok")
            return True
        time.sleep(0.5)
    _log(f"restart started but ipc not healthy yet: {reason}")
    return False


def check_once(
    *,
    home: Path,
    start_script: Path,
    bridge_log: Path,
    restart: bool = True,
) -> dict:
    state = _load_state(home)
    pids = _pgrep_bridge()
    ok, reason, status = _ipc_check(home)
    healthy = bool(pids) and ok
    detail = {
        "healthy": healthy,
        "pids": pids,
        "ipc": reason,
        "tcp_online": status.get("tcp_online"),
        "destination_hash": status.get("destination_hash"),
        "webhook_pending": status.get("webhook_pending"),
        "webhook_url_set": status.get("webhook_url_set"),
        "webhook_key_set": status.get("webhook_key_set"),
    }

    # Always try to drain the queue, even when the bridge is down
    # (queue + URL/key live on disk / in env).
    detail["webhook_retry"] = _retry_webhooks(home)

    if healthy:
        state["consecutive_failures"] = 0
        state["last_ok_at"] = time.time()
        _save_state(home, state)
        _log(
            "ok pids={0} tcp_online={1} webhook_pending={2} webhook_key_set={3}".format(
                pids,
                status.get("tcp_online"),
                status.get("webhook_pending"),
                status.get("webhook_key_set"),
            )
        )
        return detail

    failures = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = failures
    _save_state(home, state)
    _log(f"unhealthy fail={failures} pids={pids} reason={reason}")

    if not restart:
        return detail
    if failures < FAIL_THRESHOLD:
        return detail
    last = float(state.get("last_restart_at") or 0)
    if last and (time.time() - last) < RESTART_COOLDOWN_S:
        _log(f"restart skipped: cooldown {int(RESTART_COOLDOWN_S - (time.time() - last))}s left")
        return detail

    did = _clean_restart(home, start_script, bridge_log)
    state["last_restart_at"] = time.time()
    if did:
        state["consecutive_failures"] = 0
        state["last_ok_at"] = time.time()
    _save_state(home, state)
    detail["restarted"] = did
    return detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=300, help="seconds between checks (default 300)")
    parser.add_argument("--home", default=None, help="RATSPEAK_MCP_HOME")
    parser.add_argument("--start-script", default=str(DEFAULT_START))
    parser.add_argument("--bridge-log", default="/tmp/ratspeak-bridge.out")
    parser.add_argument("--no-restart", action="store_true", help="check and retry webhooks only")
    args = parser.parse_args(argv)

    if args.home:
        os.environ["RATSPEAK_MCP_HOME"] = args.home
    home = _home()
    os.environ.setdefault("RATSPEAK_MCP_HOME", str(home))

    # Load webhook URL / key from the data dir when the env is empty, without
    # printing values.
    url_file = home / "webhook_url.txt"
    if not os.environ.get("RATSPEAK_WEBHOOK_URL") and url_file.is_file():
        os.environ["RATSPEAK_WEBHOOK_URL"] = url_file.read_text(encoding="utf-8").strip()
    key_file = home / "webhook_key.txt"
    if not os.environ.get("RATSPEAK_WEBHOOK_KEY") and key_file.is_file():
        os.environ["RATSPEAK_WEBHOOK_KEY"] = key_file.read_text(encoding="utf-8").strip()

    start_script = Path(args.start_script)
    bridge_log = Path(args.bridge_log)

    def once() -> dict:
        return check_once(
            home=home,
            start_script=start_script,
            bridge_log=bridge_log,
            restart=not args.no_restart,
        )

    if not args.loop:
        once()
        return 0

    _log(f"watchdog loop interval={args.interval}s home={home}")
    while True:
        try:
            once()
        except Exception as exc:
            _log(f"watchdog tick error: {type(exc).__name__}: {exc}")
        time.sleep(max(5, int(args.interval)))


if __name__ == "__main__":
    # Allow running as a script from a venv or as python -m via path setup.
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    raise SystemExit(main())
