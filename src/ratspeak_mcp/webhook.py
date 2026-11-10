"""Inbound webhook wake for allowlisted Ratspeak peers.

Fires an HTTP POST when an inbound LXMF message arrives from an allowlisted
source hash and RATSPEAK_WEBHOOK_URL is set. Soft-fails on HTTP errors so the
mesh delivery loop never crashes.

Failed deliveries are persisted in webhook_queue.json and retried with
exponential backoff until a 2xx response. Each attempt is logged without
secrets (no URL, key, or Authorization header).

If RATSPEAK_WEBHOOK_ALLOWLIST is unset, defaults to this deploy's peer hash
60c8a4197d1de58a53f3a95a1c97f4af (document that in README).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

# Default allowlist when RATSPEAK_WEBHOOK_ALLOWLIST is unset (this deploy).
DEFAULT_WEBHOOK_ALLOWLIST = "60c8a4197d1de58a53f3a95a1c97f4af"
CONTENT_PREVIEW_MAX = 200
FIRED_HISTORY_LIMIT = 500
QUEUE_LIMIT = 200
BACKOFF_CAP_SECONDS = 900.0  # 15 minutes
DEFAULT_HOME = Path("/home/box/.ratspeak-mcp")
LOG = logging.getLogger("ratspeak-bridge")

PostFn = Callable[..., Any]


def data_home() -> Path:
    env = os.environ.get("RATSPEAK_MCP_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_HOME


def webhook_url(home: Path | None = None) -> str | None:
    url = (os.environ.get("RATSPEAK_WEBHOOK_URL") or "").strip()
    if url:
        return url
    path_raw = (os.environ.get("RATSPEAK_WEBHOOK_URL_FILE") or "").strip()
    candidates: list[Path] = []
    if path_raw:
        candidates.append(Path(path_raw).expanduser())
    base = Path(home) if home is not None else data_home()
    candidates.append(base / "webhook_url.txt")
    return _first_file_secret(candidates)


def _first_file_secret(candidates: list[Path]) -> str | None:
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                val = path.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            continue
    return None


def webhook_key(home: Path | None = None) -> str | None:
    """Return the Bearer token if configured. Never logs the value.

    Resolution order:
    1. RATSPEAK_WEBHOOK_KEY
    2. RATSPEAK_WEBHOOK_KEY_FILE (if set)
    3. <home or $RATSPEAK_MCP_HOME>/webhook_key.txt (if present)
    """
    key = (os.environ.get("RATSPEAK_WEBHOOK_KEY") or "").strip()
    if key:
        return key
    path_raw = (os.environ.get("RATSPEAK_WEBHOOK_KEY_FILE") or "").strip()
    candidates: list[Path] = []
    if path_raw:
        candidates.append(Path(path_raw).expanduser())
    base = Path(home) if home is not None else data_home()
    candidates.append(base / "webhook_key.txt")
    return _first_file_secret(candidates)


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


def queue_path(home: Path) -> Path:
    return Path(home) / "webhook_queue.json"


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
    _atomic_write(path, json.dumps({"message_ids": trimmed}, indent=2))


def already_fired(home: Path, message_id: str) -> bool:
    return message_id in set(load_fired_ids(home))


def mark_fired(home: Path, message_id: str) -> None:
    ids = load_fired_ids(home)
    if message_id not in ids:
        ids.append(message_id)
        save_fired_ids(home, ids)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def backoff_seconds(attempts: int, cap: float = BACKOFF_CAP_SECONDS) -> float:
    """Delay after `attempts` failed tries. 1, 2, 4, ... seconds, capped."""
    if attempts <= 0:
        return 0.0
    return min(cap, float(2 ** (attempts - 1)))


def _format_post_error(exc: BaseException) -> str:
    """Short, secret-free reason for logs."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        name = type(reason).__name__ if not isinstance(reason, str) else "URLError"
        return f"URLError {name}"
    return f"{type(exc).__name__}"


def post_webhook(url: str, payload: dict[str, Any], bearer: str | None = None, timeout: float = 10.0) -> int:
    """POST JSON payload. Raises on transport/HTTP errors. Returns status code on 2xx."""
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
        _ = resp.read()
        status = int(getattr(resp, "status", 0) or 200)
        if status < 200 or status >= 300:
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", resp.headers, None)
        return status


def _empty_queue() -> dict[str, Any]:
    return {"items": []}


def _normalize_queue(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        items = [i for i in raw["items"] if isinstance(i, dict) and i.get("message_id")]
        return {"items": items}
    return _empty_queue()


@contextmanager
def _locked_queue(home: Path) -> Iterator[list[dict[str, Any]]]:
    path = queue_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _atomic_write(path, json.dumps(_empty_queue(), indent=2))
    with open(path, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            try:
                fh.seek(0)
                raw = json.loads(fh.read() or "{}")
            except Exception:
                raw = _empty_queue()
            data = _normalize_queue(raw)
            items = data["items"]
            yield items
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"items": items}, indent=2))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def queue_stats(home: Path) -> dict[str, Any]:
    pending = 0
    path = queue_path(home)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            pending = len(_normalize_queue(raw)["items"])
        except Exception:
            pending = 0
    return {
        "webhook_pending": pending,
        "webhook_url_set": bool(webhook_url(home)),
        "webhook_key_set": bool(webhook_key(home)),
    }


def pending_count(home: Path) -> int:
    return int(queue_stats(home)["webhook_pending"])


def queue_has_due(home: Path, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    path = queue_path(home)
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    for item in _normalize_queue(raw)["items"]:
        if float(item.get("next_attempt_at") or 0) <= now:
            return True
    return False


def _item_from_message(
    *,
    message_id: str,
    source_hash: str | None,
    destination_hash: str | None,
    title: str,
    content: str,
    timestamp: float,
    now: float,
) -> dict[str, Any]:
    preview = content if len(content) <= CONTENT_PREVIEW_MAX else content[:CONTENT_PREVIEW_MAX]
    return {
        "message_id": message_id,
        "source_hash": source_hash,
        "destination_hash": destination_hash,
        "title": title or "",
        "content": preview,
        "timestamp": timestamp,
        "attempts": 0,
        "last_attempt_at": None,
        "next_attempt_at": now,
        "last_error": None,
    }


def enqueue_webhook(
    home: Path,
    *,
    message_id: str,
    source_hash: str | None,
    destination_hash: str | None,
    title: str,
    content: str,
    timestamp: float,
    now: float | None = None,
) -> bool:
    """Add a delivery to the on-disk queue if not already queued or fired. Returns True if added."""
    if already_fired(home, message_id):
        return False
    now = time.time() if now is None else now
    added = False
    with _locked_queue(home) as items:
        if any(i.get("message_id") == message_id for i in items):
            return False
        if already_fired(home, message_id):
            return False
        items.append(
            _item_from_message(
                message_id=message_id,
                source_hash=source_hash,
                destination_hash=destination_hash,
                title=title,
                content=content,
                timestamp=timestamp,
                now=now,
            )
        )
        overflow = len(items) - QUEUE_LIMIT
        if overflow > 0:
            dropped = items[:overflow]
            del items[:overflow]
            LOG.warning(
                "webhook queue over limit=%s; dropped %s oldest message_ids",
                QUEUE_LIMIT,
                [d.get("message_id") for d in dropped],
            )
        added = True
    return added


def _update_item_failure(item: dict[str, Any], exc: BaseException, now: float) -> None:
    attempts = int(item.get("attempts") or 0) + 1
    item["attempts"] = attempts
    item["last_attempt_at"] = now
    item["last_error"] = _format_post_error(exc)
    item["next_attempt_at"] = now + backoff_seconds(attempts)


def _deliver_item(
    item: dict[str, Any],
    *,
    url: str,
    bearer: str | None,
    post_fn: PostFn,
    log: logging.Logger,
) -> tuple[bool, str]:
    payload = build_payload(
        message_id=str(item["message_id"]),
        source_hash=item.get("source_hash"),
        destination_hash=item.get("destination_hash"),
        title=str(item.get("title") or ""),
        content=str(item.get("content") or ""),
        timestamp=float(item.get("timestamp") or 0),
    )
    attempt = int(item.get("attempts") or 0) + 1
    mid = item["message_id"]
    try:
        result = post_fn(url, payload, bearer=bearer)
        status = int(result) if isinstance(result, int) else 200
        log.info(
            "webhook attempt message_id=%s attempt=%s result=ok status=%s",
            mid,
            attempt,
            status,
        )
        return True, f"HTTP {status}"
    except Exception as exc:
        reason = _format_post_error(exc)
        log.warning(
            "webhook attempt message_id=%s attempt=%s result=fail reason=%s backoff_s=%s",
            mid,
            attempt,
            reason,
            backoff_seconds(attempt),
        )
        raise


def retry_pending_webhooks(
    home: Path,
    *,
    now: float | None = None,
    url: str | None = None,
    bearer: str | None = None,
    post_fn: PostFn | None = None,
    logger: logging.Logger | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Retry due (or all, if force) queued deliveries. Safe to call from watchdog or daemon."""
    log = logger or LOG
    now = time.time() if now is None else now
    url = webhook_url(home) if url is None else url
    if not url:
        return {"ok": False, "error": "webhook url not set", "delivered": 0, "failed": 0, "skipped": 0, "pending": pending_count(home)}

    key = webhook_key(home) if bearer is None else bearer
    do_post = post_fn or post_webhook

    with _locked_queue(home) as items:
        # Drop anything already marked fired.
        fired = set(load_fired_ids(home))
        items[:] = [i for i in items if i.get("message_id") not in fired]
        due = [
            i
            for i in items
            if force or float(i.get("next_attempt_at") or 0) <= now
        ]
        # Work on a snapshot of message_ids; mutate items after each post *outside* HTTP
        due_ids = [str(i["message_id"]) for i in due]

    delivered = 0
    failed = 0
    skipped = 0

    for mid in due_ids:
        with _locked_queue(home) as items:
            item = next((i for i in items if i.get("message_id") == mid), None)
            if item is None:
                skipped += 1
                continue
            if already_fired(home, mid):
                items[:] = [i for i in items if i.get("message_id") != mid]
                skipped += 1
                continue
            snapshot = dict(item)

        try:
            _deliver_item(snapshot, url=url, bearer=key, post_fn=do_post, log=log)
        except Exception as exc:
            failed += 1
            with _locked_queue(home) as items:
                for i in items:
                    if i.get("message_id") == mid:
                        _update_item_failure(i, exc, now)
                        break
            continue

        mark_fired(home, mid)
        with _locked_queue(home) as items:
            items[:] = [i for i in items if i.get("message_id") != mid]
        delivered += 1

    return {
        "ok": True,
        "delivered": delivered,
        "failed": failed,
        "skipped": skipped,
        "pending": pending_count(home),
    }


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
    logger: logging.Logger | None = None,
) -> bool:
    """Enqueue and attempt webhook if URL set, source allowlisted, and not yet fired.

    Returns True if a POST completed with 2xx on this call.
    Soft-fails all errors (returns False) after persisting the item for retry.
    Never raises.
    """
    log = logger or LOG
    try:
        url = webhook_url(home) if url is None else url
        if not url:
            return False

        src = (source_hash or "").strip().lower().replace(":", "")
        allow = allowlist if allowlist is not None else parse_allowlist()
        if not src or src not in allow:
            log.info(
                "webhook skip message_id=%s reason=not_allowlisted",
                message_id,
            )
            return False

        if already_fired(home, message_id):
            log.info("webhook skip message_id=%s reason=already_fired", message_id)
            return False

        enqueue_webhook(
            home,
            message_id=message_id,
            source_hash=source_hash,
            destination_hash=destination_hash,
            title=title or "",
            content=content or "",
            timestamp=timestamp,
        )
        result = retry_pending_webhooks(
            home,
            url=url,
            bearer=bearer,
            post_fn=post_fn,
            logger=log,
        )
        return bool(result.get("delivered")) and already_fired(home, message_id)
    except Exception as exc:
        log.warning(
            "webhook fire error message_id=%s reason=%s",
            message_id,
            _format_post_error(exc),
        )
        return False


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retry or inspect the Ratspeak webhook queue.")
    parser.add_argument("--retry", action="store_true", help="retry due queued deliveries")
    parser.add_argument("--force", action="store_true", help="retry all queued items, ignoring backoff")
    parser.add_argument("--status", action="store_true", help="print queue stats as JSON")
    parser.add_argument("--home", default=None, help="override RATSPEAK_MCP_HOME")
    args = parser.parse_args(argv)
    if args.home:
        os.environ["RATSPEAK_MCP_HOME"] = args.home
    home = data_home()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.retry or args.force:
        result = retry_pending_webhooks(home, force=bool(args.force))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.status:
        print(json.dumps(queue_stats(home), sort_keys=True))
        return 0
    parser.print_help()
    return 2


def main() -> None:
    raise SystemExit(_cli())


if __name__ == "__main__":
    main()
