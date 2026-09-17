"""Always-on Ratspeak bridge daemon (presence + optional webhook wake).

Starts RatspeakBridge, re-announces about every 25 minutes, and stays alive
forever. File logging under the data home; private keys are never written to
stdout or log lines.

Also serves a local Unix send IPC socket (`send.sock`) so clients can outbound
LXMF without stopping the daemon.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from ratspeak_mcp.bridge import RatspeakBridge, data_home
from ratspeak_mcp.send_ipc import SendIpcServer

REANNOUNCE_SECONDS = 25 * 60  # ~25 minutes
HEARTBEAT_SECONDS = 30
LOG_NAME = "ratspeak-bridge"


def _setup_logging(home: Path) -> logging.Logger:
    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "daemon.log"
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Minimal stdout: status only, never secrets / private keys
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


def main() -> None:
    home = data_home()
    log = _setup_logging(home)
    display = os.environ.get("RATSPEAK_MCP_DISPLAY_NAME", "Rat")
    host = os.environ.get("RATSPEAK_MCP_TCP_HOST", "2.ratspeak.org")
    port = int(os.environ.get("RATSPEAK_MCP_TCP_PORT", "4242"))

    log.info(
        "ratspeak-bridge starting home=%s display=%s tcp=%s:%s webhook_url_set=%s",
        home,
        display,
        host,
        port,
        bool((os.environ.get("RATSPEAK_WEBHOOK_URL") or "").strip()),
    )

    bridge = RatspeakBridge(display_name=display, tcp_host=host, tcp_port=port, home=home)
    try:
        bridge.start(announce=True, wait_tcp_seconds=8.0)
    except Exception as e:
        log.error("bridge start failed: %s", e)
        raise SystemExit(1) from e

    status = bridge.status()
    dest = status.get("destination_hash")
    log.info(
        "bridge up destination_hash=%s tcp_online=%s inbox_count=%s",
        dest,
        status.get("tcp_online"),
        status.get("inbox_count"),
    )
    if dest:
        # Safe public identity only
        print(f"ratspeak-bridge ready destination_hash={dest}", flush=True)

    ipc = SendIpcServer(bridge, home=home, logger=log)
    try:
        ipc.start()
    except Exception as e:
        log.error("send IPC failed to start: %s", e)
        # Presence still useful even if IPC bind fails
        ipc = None

    stop = {"flag": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("received signal %s; shutting down", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    last_announce = time.time()
    try:
        while not stop["flag"]:
            time.sleep(HEARTBEAT_SECONDS)
            if stop["flag"]:
                break
            now = time.time()
            if now - last_announce >= REANNOUNCE_SECONDS:
                try:
                    result = bridge.announce()
                    log.info(
                        "re-announce ok destination_hash=%s tcp_online=%s",
                        result.get("destination_hash"),
                        result.get("tcp_online"),
                    )
                except Exception as e:
                    log.warning("re-announce failed: %s", e)
                last_announce = now
    finally:
        if ipc is not None:
            try:
                ipc.stop()
            except Exception as e:
                log.warning("send IPC stop error: %s", e)

    log.info("ratspeak-bridge stopped")


if __name__ == "__main__":
    main()
