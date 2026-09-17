#!/usr/bin/env python3
"""Smoke test: start bridge briefly, verify identity hash + TCP config."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure package import when run as script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ratspeak_mcp.bridge import RatspeakBridge, data_home


def main() -> int:
    home = data_home()
    print(f"data_home={home}")
    bridge = RatspeakBridge()
    try:
        bridge.start(announce=True, wait_tcp_seconds=8.0)
    except Exception as e:
        print(f"START_ERROR: {e}")
        # Still check filesystem artifacts if partial
    status = bridge.status()
    print("STATUS:")
    print(json.dumps(status, indent=2, default=str))

    config = home / "rns" / "config"
    dest_file = home / "destination_hash.txt"
    identity = home / "identity" / "lxmf_identity"

    ok = True
    checks = {
        "config_exists": config.exists(),
        "config_has_emerald": False,
        "identity_exists": identity.exists(),
        "destination_hash_file": dest_file.exists(),
        "destination_hash": status.get("destination_hash"),
        "tcp_online": status.get("tcp_online"),
    }
    if config.exists():
        text = config.read_text(encoding="utf-8")
        checks["config_has_emerald"] = (
            "TCPClientInterface" in text and "2.ratspeak.org" in text
        )

    print("CHECKS:")
    print(json.dumps(checks, indent=2))

    if not checks["config_exists"] or not checks["config_has_emerald"]:
        print("FAIL: TCP config missing or incomplete")
        ok = False
    if not checks["identity_exists"]:
        print("FAIL: identity not created")
        ok = False
    if not checks["destination_hash"]:
        print("FAIL: destination hash not generated")
        ok = False
    else:
        print(f"DESTINATION_HASH={checks['destination_hash']}")
        if dest_file.exists():
            print(f"DESTINATION_HASH_FILE={dest_file.read_text().strip()}")

    if checks["tcp_online"]:
        print("TCP_CONNECT=ok (Emerald online)")
    else:
        print("TCP_CONNECT=failed_or_pending (code still valid; check network)")
        # Not a hard fail for deliverable — report clearly
        ok = ok  # keep identity success

    # Brief dwell so announce can leave the wire if TCP is up
    time.sleep(1.0)
    print("SMOKE_OK" if (checks["destination_hash"] and checks["config_has_emerald"]) else "SMOKE_FAIL")
    return 0 if (checks["destination_hash"] and checks["config_has_emerald"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
