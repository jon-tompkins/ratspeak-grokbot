"""Inbound webhook wake for allowlisted Ratspeak peers.

Fires an HTTP POST when an inbound LXMF message arrives from an allowlisted
source hash and RATSPEAK_WEBHOOK_URL is set. Soft-fails on HTTP errors so the
mesh delivery loop never crashes.

If RATSPEAK_WEBHOOK_ALLOWLIST is unset, defaults to this deploy's peer hash
60c8a4197d1de58a53f3a95a1c97f4af (document that in README).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Default allowlist when RATSPEAK_WEBHOOK_ALLOWLIST is unset (this deploy).
DEFAULT_WEBHOOK_ALLOWLIST = "60c8a4197d1de58a53f3a95a1c97f4af"
CONTENT_PREVIEW_MAX = 200
FIRED_HISTORY_LIMIT = 500


def webhook_url() -> str | None:
    url = (os.environ.get("RATSPEAK_WEBHOOK_URL") or "").strip()
    return url or None


def webhook_key() -> str | None:
    key = (os.environ.get("RATSPEAK_WEBHOOK_KEY") or "").strip()
    return key or None


def parse_allowlist(raw: str | None = None) -> set[str]:
    """Parse comma-separated hex hashes; empty/None uses DEFAULT_WEBHOOK_ALLOWLIST."""
    if raw is None:
        raw = os.environ.get("RATSPEAK_WEBHOOK_ALLOWLIST")
    if raw is None or not str(raw).strip():
        return {DEFAULT_WEBHOOK_ALLOWLIST.lower()}
    out: set[str] = set()
    for part in str(raw).split(","):
        h = part.strip().lower().replace(":", "")
        if h:
            out.add(h)
    return out if out else {DEFAULT_WEBHOOK_ALLOWLIST.lower()}


def fired_path(home: Path) -> Path:
    return Path(home) / "webhook_fired.json"


def load_fired_ids(home: Path) -> list[str]:
    path = fired_path(home)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, dict) and isinstance(raw.get("message_ids"), list):
            return [str(x) for x in raw["message_ids"]]
    except Exception:
        pass
    return []


def save_fired_ids(home: Path, ids: list[str]) -> None:
    path = fired_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = ids[-FIRED_HISTORY_LIMIT:]
    path.write_text(json.dumps({"message_ids": trimmed}, indent=2), encoding="utf-8")


def already_fired(home: Path, message_id: str) -> bool:
    return message_id in set(load_fired_ids(home))


def mark_fired(home: Path, message_id: str) -> None:
    ids = load_fired_ids(home)
    if message_id not in ids:
        ids.append(message_id)
        save_fired_ids(home, ids)


def build_payload(
    *,
    message_id: str,
    source_hash: str | None,
    destination_hash: str | None,
    title: str,
    content: str,
    timestamp: float,
) -> dict[str, Any]:
    preview = content if len(content) <= CONTENT_PREVIEW_MAX else content[:CONTENT_PREVIEW_MAX]
    return {
        "event": "ratspeak.inbound",
        "message_id": message_id,
        "source_hash": source_hash,
        "destination_hash": destination_hash,
        "title": title,
        "content_preview": preview,
        "timestamp": timestamp,
    }


def post_webhook(url: str, payload: dict[str, Any], bearer: str | None = None, timeout: float = 10.0) -> None:
    """POST JSON payload. Raises on transport/HTTP errors (caller soft-fails)."""
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ratspeak-mcp-webhook/0.1",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Consume body; treat non-2xx as error via HTTPError from urlopen
        _ = resp.read()


def maybe_fire_inbound_webhook(
    *,
    home: Path,
    message_id: str,
    source_hash: str | None,
    destination_hash: str | None,
    title: str,
    content: str,
    timestamp: float,
    allowlist: set[str] | None = None,
    url: str | None = None,
    bearer: str | None = None,
    post_fn=None,
) -> bool:
    """Fire webhook if URL set, source allowlisted, and message_id not yet fired.

    Returns True if a POST was attempted and completed without exception.
    Soft-fails all errors (returns False). Never raises.
    """
    try:
        url = webhook_url() if url is None else url
        if not url:
            return False

        src = (source_hash or "").strip().lower().replace(":", "")
        allow = allowlist if allowlist is not None else parse_allowlist()
        if not src or src not in allow:
            return False

        if already_fired(home, message_id):
            return False

        payload = build_payload(
            message_id=message_id,
            source_hash=source_hash,
            destination_hash=destination_hash,
            title=title or "",
            content=content or "",
            timestamp=timestamp,
        )
        key = webhook_key() if bearer is None else bearer
        do_post = post_fn or post_webhook
        do_post(url, payload, bearer=key)
        mark_fired(home, message_id)
        return True
    except Exception:
        # Soft-fail: never crash mesh loop
        return False
