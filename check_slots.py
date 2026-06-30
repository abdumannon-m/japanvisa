#!/usr/bin/env python3
"""Watch Japan visa appointment slots and notify via Telegram."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Any


BASE = "https://uzembassyryouji.rsvsys.jp/reservations/calendar"
AJAX_URL = "https://uzembassyryouji.rsvsys.jp/ajax/reservations/calendar"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
NEG = [
    "not available",
    "unavailable",
    "qabul tugadi",
    "qabul yakunlandi",
    "приём окончен",
    "прием окончен",
    "受付終了",
    "受付は終了",
]
# Latin-script positive keywords must match on word boundaries so that, for
# example, "available" never matches inside "unavailable".
POS_WORD = [
    "few",
    "available",
]
# CJK/Cyrillic keywords have no usable word boundaries, so match as substrings.
POS_SUBSTRING = [
    "qabul qilinmoqda",
    "ведётся",
    "ведется",
    "受付中",
    "残りわずか",
]
# Retained for backwards compatibility / introspection.
POS = POS_WORD + POS_SUBSTRING


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[warn] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default
    if minimum is not None and value < minimum:
        print(f"[warn] {name}={value} below minimum {minimum}; using {default}", file=sys.stderr)
        return default
    if maximum is not None and value > maximum:
        print(f"[warn] {name}={value} above maximum {maximum}; using {default}", file=sys.stderr)
        return default
    return value


def get_config() -> dict[str, Any]:
    event_id = env_str("EVENT_ID", "20")
    telegram_chat_id = env_str("TELEGRAM_CHAT_ID")
    # TELEGRAM_CHAT_ID may be a comma-separated list of fixed recipients who
    # always receive alerts (in addition to anyone who /subscribes).
    telegram_chat_ids = [part.strip() for part in telegram_chat_id.split(",") if part.strip()]
    return {
        "event_id": event_id,
        "category_id": env_str("CATEGORY_ID", "12"),
        "plan_id": env_str("PLAN_ID", "19"),
        "months_ahead": env_int("MONTHS_AHEAD", 2, minimum=0),
        "event_label": env_str("EVENT_LABEL", "Short stay - Applicant"),
        "month_param": env_str("MONTH_PARAM", "date"),
        "status_interval_seconds": env_int("STATUS_INTERVAL_SECONDS", 3600, minimum=60),
        "state_file": Path(env_str("STATE_FILE", "state.json")),
        "state_key": env_str("STATE_KEY", f"event-{event_id}"),
        "supabase_url": env_str("SUPABASE_URL").rstrip("/"),
        "supabase_service_key": env_str("SUPABASE_SERVICE_KEY"),
        "upstash_redis_rest_url": env_str("UPSTASH_REDIS_REST_URL").rstrip("/"),
        "upstash_redis_rest_token": env_str("UPSTASH_REDIS_REST_TOKEN"),
        "telegram_bot_token": env_str("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": telegram_chat_id,
        "telegram_chat_ids": telegram_chat_ids,
        "telegram_webhook_secret": env_str("TELEGRAM_WEBHOOK_SECRET"),
    }


def is_open(alt: str | None) -> bool:
    text = (alt or "").lower()
    # Evaluate the negative list first: a closed day must never read as open.
    if any(part in text for part in NEG):
        return False
    matched = any(re.search(rf"\b{re.escape(part)}\b", text) for part in POS_WORD)
    matched = matched or any(part in text for part in POS_SUBSTRING)
    if matched:
        return True
    # The alt text matched neither list. Treat as closed (safe default) but warn
    # so future site wording drift is visible instead of silently swallowed.
    stripped = text.strip()
    if stripped:
        print(f"[warn] Unrecognized slot alt text (treating as closed): {alt!r}", file=sys.stderr)
    return False


# The status-icon FILENAME is the authoritative availability signal on this site.
# The <img alt> text is INVERTED relative to reality and must never be trusted on
# its own: an OPEN day renders icon_circle.svg yet carries alt
# "Not available / Qabul tugadi / Приём окончен", while a CLOSED day renders
# icon_disabled.svg yet carries alt "Available / Qabul qilinmoqda / Приём ведётся".
# Verified against a real open slot captured live on 2026-06-30.
OPEN_ICON_MARKERS = ("icon_circle", "icon_triangle")  # ○ available, △ few remaining
CLOSED_ICON_MARKERS = ("icon_disabled", "icon_cross", "icon_batsu")  # greyed / × full
ICON_STATUS_LABELS = {
    "icon_circle": "Available",
    "icon_triangle": "Few remaining",
}


def icon_is_open(src: str | None) -> bool | None:
    """Classify a day from its status-icon filename.

    Returns True (open), False (closed), or None when the icon is unrecognized.
    """
    name = (src or "").lower()
    if any(marker in name for marker in OPEN_ICON_MARKERS):
        return True
    if any(marker in name for marker in CLOSED_ICON_MARKERS):
        return False
    return None


def icon_status_label(src: str | None, fallback: str = "Available") -> str:
    """Human-readable status for an open day (the raw alt text is inverted)."""
    name = (src or "").lower()
    for marker, label in ICON_STATUS_LABELS.items():
        if marker in name:
            return label
    return fallback


def slot_is_open(src: str | None, alt: str | None) -> bool:
    """Decide whether a calendar day is open, preferring the icon over alt text."""
    verdict = icon_is_open(src)
    if verdict is not None:
        return verdict
    # Unknown icon: the alt text on this site is inverted/unreliable, so we cannot
    # safely treat it as open. Warn (so future icon changes surface) and fail closed.
    if (src or "").strip():
        print(
            f"[warn] Unrecognized slot icon (treating as closed): src={src!r} alt={alt!r}",
            file=sys.stderr,
        )
        return False
    # No icon src at all (legacy/synthetic markup): last-resort alt heuristic.
    return is_open(alt)


def parse_year_month(text: str) -> str:
    match = re.search(r"(\d{4})\s*年\s*0?(\d{1,2})\s*月", text)
    if not match:
        raise ValueError(f"Could not parse calendar month from {text!r}")
    year, month = match.groups()
    return f"{int(year):04d}-{int(month):02d}"


def booking_url(event_id: str, category_id: str | None = None) -> str:
    query = {"event": event_id}
    if category_id:
        query["category"] = category_id
    return f"{BASE}?{urllib.parse.urlencode(query)}"


def configured_booking_url(config: dict[str, Any]) -> str:
    return booking_url(str(config["event_id"]), str(config["category_id"]))


def import_requests() -> Any:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install requests with `python -m pip install -r requirements.txt`") from exc
    return requests


# Per-request network timeout (seconds). Kept short so total wall time across
# retries stays well under a 60s serverless limit.
HTTP_TIMEOUT = 15
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 8.0


def _retry_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction for requests/urllib/telegram errors."""
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


def _is_transient(exc: Exception) -> bool:
    status = _retry_status_code(exc)
    if status is not None:
        return status == 429 or 500 <= status <= 599
    # Network/timeout/connection errors are transient. We avoid importing
    # requests at module import time, so match on class name as a fallback.
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    name = exc.__class__.__name__
    return name in {"Timeout", "ConnectionError", "ConnectTimeout", "ReadTimeout", "ChunkedEncodingError"}


def with_retries(func: Any, *, what: str, attempts: int = RETRY_ATTEMPTS) -> Any:
    """Call ``func`` with bounded exponential backoff on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not transient
            last_exc = exc
            if attempt >= attempts or not _is_transient(exc):
                raise
            delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
            print(
                f"[warn] {what} transient failure (attempt {attempt}/{attempts}): {exc}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def supabase_enabled(config: dict[str, Any]) -> bool:
    return bool(config["supabase_url"] and config["supabase_service_key"])


def upstash_enabled(config: dict[str, Any]) -> bool:
    return bool(config["upstash_redis_rest_url"] and config["upstash_redis_rest_token"])


def remote_state_enabled(config: dict[str, Any]) -> bool:
    return upstash_enabled(config) or supabase_enabled(config)


def supabase_headers(config: dict[str, Any]) -> dict[str, str]:
    service_key = str(config["supabase_service_key"])
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def supabase_state_url(config: dict[str, Any]) -> str:
    return f"{config['supabase_url']}/rest/v1/visa_slot_state"


def telegram_state_key(config: dict[str, Any]) -> str:
    return f"{config['state_key']}:telegram"


def telegram_subscribers_key(config: dict[str, Any]) -> str:
    return f"{config['state_key']}:telegram:subscribers"


def empty_state() -> dict[str, Any]:
    return {
        "open_dates": set(),
        "open_day_alts": {},
        "telegram_update_offset": None,
        "subscribed_chats": set(),
        "last_status_at": None,
    }


def normalize_open_days(open_days: set[str] | dict[str, str]) -> dict[str, str]:
    if isinstance(open_days, dict):
        return {str(key): str(value) for key, value in open_days.items()}
    return {str(date_key): "" for date_key in open_days}


def telegram_state_value(
    telegram_update_offset: int | None,
    subscribed_chats: set[str] | None,
    last_status_at: str | None,
) -> dict[str, Any]:
    """Single source of truth for the persisted telegram sub-state shape."""
    return {
        "telegram_update_offset": telegram_update_offset,
        "subscribed_chats": sorted(subscribed_chats or set()),
        "last_status_at": last_status_at,
    }


def apply_open_days(state: dict[str, Any], open_days: Any) -> None:
    if isinstance(open_days, dict):
        # Preserve the {date: alt} mapping so the real slot status survives a
        # round-trip through state; also expose the set for date arithmetic.
        state["open_day_alts"] = {str(key): str(value) for key, value in open_days.items()}
        state["open_dates"] = {str(item) for item in open_days}
    elif isinstance(open_days, list):
        state["open_day_alts"] = {str(item): "" for item in open_days}
        state["open_dates"] = {str(item) for item in open_days}


def apply_telegram_state(state: dict[str, Any], telegram_state: Any) -> None:
    if not isinstance(telegram_state, dict):
        return
    raw_offset = telegram_state.get("telegram_update_offset")
    if isinstance(raw_offset, int):
        state["telegram_update_offset"] = raw_offset
    raw_chats = telegram_state.get("subscribed_chats", [])
    if isinstance(raw_chats, list):
        state["subscribed_chats"] = {str(item) for item in raw_chats}
    raw_last_status_at = telegram_state.get("last_status_at")
    if isinstance(raw_last_status_at, str):
        state["last_status_at"] = raw_last_status_at


def upstash_headers(config: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config['upstash_redis_rest_token']}",
        "Content-Type": "application/json",
    }


def upstash_command(config: dict[str, Any], *args: Any) -> Any:
    requests = import_requests()

    def call() -> Any:
        response = requests.post(
            str(config["upstash_redis_rest_url"]),
            headers=upstash_headers(config),
            data=json.dumps(list(args)),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response

    response = with_retries(call, what="Upstash command")
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"Upstash command failed: {payload['error']}")
    if not isinstance(payload, dict) or "result" not in payload:
        raise RuntimeError("Upstash command failed: unexpected response")
    return payload["result"]


def read_upstash_value(config: dict[str, Any], key: str) -> Any:
    raw = upstash_command(config, "GET", key)
    if raw in (None, ""):
        return {}
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Upstash value for {key} is not valid JSON") from exc


def upsert_upstash_value(config: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    upstash_command(config, "SET", key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def upstash_read_subscribers(config: dict[str, Any]) -> set[str]:
    result = upstash_command(config, "SMEMBERS", telegram_subscribers_key(config))
    if isinstance(result, list):
        return {str(item) for item in result}
    return set()


def upstash_add_subscriber(config: dict[str, Any], chat_id: str) -> None:
    upstash_command(config, "SADD", telegram_subscribers_key(config), str(chat_id))


def upstash_remove_subscriber(config: dict[str, Any], chat_id: str) -> None:
    upstash_command(config, "SREM", telegram_subscribers_key(config), str(chat_id))


def apply_subscriber_delta(config: dict[str, Any], added: set[str], removed: set[str]) -> None:
    """Atomically apply subscribe/unsubscribe deltas using Redis SET ops."""
    for chat_id in sorted(added):
        upstash_add_subscriber(config, chat_id)
    for chat_id in sorted(removed):
        upstash_remove_subscriber(config, chat_id)


def read_upstash_state(config: dict[str, Any]) -> dict[str, Any]:
    state = empty_state()
    apply_open_days(state, read_upstash_value(config, str(config["state_key"])))
    apply_telegram_state(state, read_upstash_value(config, telegram_state_key(config)))
    # Subscribers live in an atomic Redis SET. Prefer it; fall back to the JSON
    # blob (legacy/migration) when the SET is empty.
    set_chats = upstash_read_subscribers(config)
    if set_chats:
        state["subscribed_chats"] = set_chats
    return state


def save_upstash_state(
    config: dict[str, Any],
    open_days: set[str] | dict[str, str],
    telegram_update_offset: int | None = None,
    subscribed_chats: set[str] | None = None,
    last_status_at: str | None = None,
) -> None:
    upsert_upstash_value(config, str(config["state_key"]), normalize_open_days(open_days))
    upsert_upstash_value(
        config,
        telegram_state_key(config),
        telegram_state_value(telegram_update_offset, subscribed_chats, last_status_at),
    )


def read_supabase_value(config: dict[str, Any], key: str) -> Any:
    requests = import_requests()

    def call() -> Any:
        response = requests.get(
            supabase_state_url(config),
            headers=supabase_headers(config),
            params={"key": f"eq.{key}", "select": "value"},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response

    response = with_retries(call, what="Supabase read")
    rows = response.json()
    if not rows:
        return {}
    return rows[0].get("value", {})


def read_supabase_state(config: dict[str, Any]) -> dict[str, Any]:
    state = empty_state()
    apply_open_days(state, read_supabase_value(config, str(config["state_key"])))
    apply_telegram_state(state, read_supabase_value(config, telegram_state_key(config)))
    return state


def upsert_supabase_value(config: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    requests = import_requests()
    payload = {
        "key": str(key),
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    headers = supabase_headers(config)
    headers["Prefer"] = "resolution=merge-duplicates"

    def call() -> Any:
        response = requests.post(
            supabase_state_url(config),
            headers=headers,
            params={"on_conflict": "key"},
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response

    with_retries(call, what="Supabase write")


def save_supabase_state(
    config: dict[str, Any],
    open_days: set[str] | dict[str, str],
    telegram_update_offset: int | None = None,
    subscribed_chats: set[str] | None = None,
    last_status_at: str | None = None,
) -> None:
    upsert_supabase_value(config, str(config["state_key"]), normalize_open_days(open_days))
    upsert_supabase_value(
        config,
        telegram_state_key(config),
        telegram_state_value(telegram_update_offset, subscribed_chats, last_status_at),
    )


def state_label(config: dict[str, Any]) -> str:
    if upstash_enabled(config):
        return f"upstash:{config['state_key']}"
    if supabase_enabled(config):
        return f"supabase:{config['state_key']}"
    return str(config["state_file"])


def warn_if_ephemeral_state(config: dict[str, Any]) -> None:
    """Loudly warn when running without durable state (ephemeral on serverless)."""
    if not remote_state_enabled(config):
        print(
            "[warn] No durable state backend configured (UPSTASH/SUPABASE unset); "
            f"using local file {config['state_file']} which is EPHEMERAL on serverless "
            "and will not persist across invocations — slot dedup/alerts may repeat or be lost.",
            file=sys.stderr,
        )


def read_state(path: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or get_config()
    if upstash_enabled(config):
        return read_upstash_state(config)
    if supabase_enabled(config):
        return read_supabase_state(config)

    state = empty_state()

    if not path.exists():
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] Could not read state file {path}: {exc}", file=sys.stderr)
        return state

    if isinstance(data, list):
        state["open_dates"] = {str(item) for item in data}
        state["open_day_alts"] = {str(item): "" for item in data}
        return state
    if not isinstance(data, dict):
        return state

    raw_alts = data.get("open_day_alts")
    if isinstance(raw_alts, dict):
        state["open_day_alts"] = {str(key): str(value) for key, value in raw_alts.items()}
        state["open_dates"] = {str(key) for key in raw_alts}
    else:
        raw_dates = data.get("open_dates", [])
        if isinstance(raw_dates, list):
            state["open_dates"] = {str(item) for item in raw_dates}
            state["open_day_alts"] = {str(item): "" for item in raw_dates}

    raw_offset = data.get("telegram_update_offset")
    if isinstance(raw_offset, int):
        state["telegram_update_offset"] = raw_offset

    raw_chats = data.get("subscribed_chats", [])
    if isinstance(raw_chats, list):
        state["subscribed_chats"] = {str(item) for item in raw_chats}

    raw_last_status_at = data.get("last_status_at")
    if isinstance(raw_last_status_at, str):
        state["last_status_at"] = raw_last_status_at

    return state


def load_state(path: Path, config: dict[str, Any] | None = None) -> set[str]:
    return set(read_state(path, config)["open_dates"])


def save_state(
    path: Path,
    open_days: set[str] | dict[str, str],
    telegram_update_offset: int | None = None,
    subscribed_chats: set[str] | None = None,
    config: dict[str, Any] | None = None,
    last_status_at: str | None = None,
) -> None:
    config = config or get_config()
    if upstash_enabled(config):
        save_upstash_state(config, open_days, telegram_update_offset, subscribed_chats, last_status_at)
        return
    if supabase_enabled(config):
        save_supabase_state(config, open_days, telegram_update_offset, subscribed_chats, last_status_at)
        return

    open_day_alts = normalize_open_days(open_days)
    payload = {
        "open_dates": sorted(open_day_alts),
        "open_day_alts": open_day_alts,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **telegram_state_value(telegram_update_offset, subscribed_chats, last_status_at),
    }
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return collapse_ws(" ".join(self.parts))


class CalendarCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[dict[str, Any]] = []
        self.stack: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value for name, value in attrs}
        if tag in {"td", "li"}:
            self.cells.append({"parts": [], "alts": [], "srcs": []})
            self.stack.append((tag, len(self.cells) - 1))
            return
        if tag == "img" and self.stack:
            alt = attr_map.get("alt")
            if alt:
                cell = self.cells[self.stack[-1][1]]
                cell["alts"].append(alt)
                # The icon filename is the authoritative status signal; keep it
                # alongside the (unreliable) alt text so callers can prefer it.
                cell["srcs"].append(attr_map.get("src") or "")

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.cells[self.stack[-1][1]]["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def html_to_text(html_doc: str) -> str:
    parser = TextExtractor()
    parser.feed(html_doc)
    return parser.text()


def extract_html(response_text: str) -> str:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text

    candidates: list[tuple[int, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            score = (
                value.lower().count("<td")
                + value.lower().count("<li")
                + value.lower().count("<img")
                + value.count("年")
            )
            if "<" in value and ">" in value:
                candidates.append((score, value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    if not candidates:
        return response_text
    return max(candidates, key=lambda item: item[0])[1]


def parse_month_from_html(html_doc: str) -> str:
    return parse_year_month(html_to_text(html_doc))


def scan_calendar_html(html_doc: str) -> dict[str, Any]:
    parser = CalendarCellParser()
    parser.feed(html_doc)
    items: list[dict[str, str | None]] = []
    for cell in parser.cells:
        if not cell["alts"]:
            continue
        text = collapse_ws(" ".join(cell["parts"]))
        day_match = re.search(r"\b(\d{1,2})\b", text)
        day = day_match.group(1) if day_match else None
        srcs = cell.get("srcs") or []
        for index, alt in enumerate(cell["alts"]):
            src = srcs[index] if index < len(srcs) else ""
            items.append({"day": day, "alt": alt, "src": src})
    return {"text": html_to_text(html_doc), "items": items}


def add_months(month: str, offset: int) -> str:
    year_text, month_text = month.split("-", maxsplit=1)
    year = int(year_text)
    month_index = int(month_text) - 1 + offset
    year += month_index // 12
    month_number = month_index % 12 + 1
    return f"{year:04d}-{month_number:02d}"


def month_param_value(month: str) -> str:
    year, month_number = month.split("-", maxsplit=1)
    return f"{year}/{month_number}/01"


def ajax_headers(config: dict[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://uzembassyryouji.rsvsys.jp",
        "Referer": configured_booking_url(config),
        "User-Agent": USER_AGENT,
    }


class CalendarFormParser(HTMLParser):
    """Collect the input fields of every <form> on the booking page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if tag == "form":
            self._current = {}
            self.forms.append(self._current)
        elif tag in {"input", "select", "textarea"} and self._current is not None:
            name = attr_map.get("name")
            if name and name not in self._current:
                self._current[name] = attr_map.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def extract_calendar_form(page_html: str, event_id: str) -> dict[str, str] | None:
    """Return the booking page's calendar search form, including its CakePHP token.

    The site is a CakePHP app whose SecurityComponent requires the form's
    _Token[fields]/_Token[unlocked] to be POSTed verbatim. Without them the AJAX
    endpoint returns the application-type chooser instead of the calendar grid
    (which is why a hand-built POST silently saw zero open days).
    """
    parser = CalendarFormParser()
    parser.feed(page_html)
    for form in parser.forms:
        if form.get("event") == str(event_id) and form.get("_Token[fields]"):
            return form
    for form in parser.forms:
        if "disp_type" in form and form.get("_Token[fields]"):
            return form
    return None


def new_calendar_session(config: dict[str, Any]) -> tuple[Any, dict[str, str], str]:
    requests = import_requests()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    def call() -> Any:
        response = session.get(configured_booking_url(config), timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response

    response = with_retries(call, what="calendar GET")
    if not session.cookies.get("USERSESSID") or not session.cookies.get("csrfToken"):
        raise RuntimeError("Initial calendar GET did not set required USERSESSID/csrfToken cookies")
    form = extract_calendar_form(response.text, str(config["event_id"]))
    if not form:
        raise RuntimeError(
            "Could not find the calendar form (with CakePHP _Token) on the booking page; "
            "the embassy site layout may have changed"
        )
    return session, form, response.text


def fetch_calendar_html(
    session: Any,
    config: dict[str, Any],
    form: dict[str, str],
    target_month: str | None = None,
) -> tuple[str, str]:
    # Replay the booking page's own calendar form (including its CakePHP _Token);
    # only the target month (the form's `date` field) changes between requests.
    data = dict(form)
    data["search"] = "exec"
    if target_month:
        data["date"] = month_param_value(target_month)

    def call() -> Any:
        response = session.post(AJAX_URL, headers=ajax_headers(config), data=data, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response

    response = with_retries(call, what="calendar POST")
    return extract_html(response.text), response.text


def redacted_cookies(session: Any) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in session.cookies.items():
        cookies[str(name)] = f"<present len={len(str(value))}>"
    return cookies


def run_probe() -> None:
    config = get_config()
    session, form, page_html = new_calendar_session(config)
    print(f"[probe] GET {configured_booking_url(config)}")
    print(f"[probe] cookies={json.dumps(redacted_cookies(session), sort_keys=True)}")
    print(f"[probe] calendar form fields={sorted(form.keys())}")
    page_scan = scan_calendar_html(page_html)
    print(f"[probe] initial GET icons={len(page_scan['items'])}")
    for item in page_scan["items"][:20]:
        alt = str(item.get("alt") or "")
        src = str(item.get("src") or "")
        icon = src.rsplit("/", 1)[-1]
        print(f"[probe]   GET day={item.get('day')} open={slot_is_open(src, alt)} icon={icon!r} alt={alt!r}")

    html_doc, raw_response = fetch_calendar_html(session, config, form)
    month = parse_month_from_html(html_doc)
    scan = scan_calendar_html(html_doc)
    print(f"[probe] parsed_month={month} icons={len(scan['items'])}")
    for item in scan["items"][:60]:
        alt = str(item.get("alt") or "")
        src = str(item.get("src") or "")
        icon = src.rsplit("/", 1)[-1]
        print(f"[probe]   day={item.get('day')} open={slot_is_open(src, alt)} icon={icon!r} alt={alt!r}")
    print("[probe] raw AJAX response (first 1500 chars) follows")
    print(raw_response[:1500])


def log_month(month: str, scan: dict[str, Any], current_open: dict[str, str]) -> None:
    open_dates: list[str] = []
    icon_count = 0
    for item in scan["items"]:
        icon_count += 1
        day = item.get("day")
        alt = item.get("alt") or ""
        src = item.get("src") or ""
        if not day or not slot_is_open(str(src), str(alt)):
            continue
        date_key = f"{month}-{int(str(day)):02d}"
        open_dates.append(date_key)
        # Store a corrected, human-readable status (the raw alt text is inverted).
        current_open[date_key] = icon_status_label(str(src), fallback=str(alt))

    unique_open = sorted(set(open_dates))
    open_log = ",".join(unique_open) if unique_open else "none"
    print(f"[{month}] icons={icon_count} open={open_log}", flush=True)


def run_once() -> dict[str, str]:
    config = get_config()
    session, form, page_html = new_calendar_session(config)
    current_open: dict[str, str] = {}
    previous_month: str | None = None
    start_month: str | None = None

    initial_month = parse_month_from_html(page_html)
    initial_scan = scan_calendar_html(page_html)
    log_month(initial_month, initial_scan, current_open)
    start_month = initial_month
    previous_month = initial_month

    for index in range(1, config["months_ahead"] + 1):
        target_month = add_months(start_month, index) if start_month else None
        html_doc, _raw = fetch_calendar_html(session, config, form, target_month)
        month = parse_month_from_html(html_doc)
        if previous_month is not None and month == previous_month:
            print("[warn] calendar month did not advance — date navigation failed", file=sys.stderr)
            break

        scan = scan_calendar_html(html_doc)
        log_month(month, scan, current_open)
        previous_month = month

    return current_open


def build_alert_message(date_keys: list[str], current: dict[str, str], config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    count = len(date_keys)
    headline = "JAPAN VISA SLOT OPEN" if count == 1 else f"{count} JAPAN VISA SLOTS OPEN"
    lines = [
        f"🚨🚨 <b>{headline}</b> 🚨🚨",
        "",
        f"📋 {label}",
        "",
        "🗓 <b>Newly available — book now:</b>",
    ]
    for date_key in date_keys:
        lines.append(f"   ✅ <b>{html.escape(date_key)}</b> — {html.escape(current[date_key])}")
    lines.extend(
        [
            "",
            f"⚡️ <a href=\"{escaped_url}\"><b>OPEN BOOKING CALENDAR</b></a>",
            "",
            f"🕒 <i>Detected {timestamp}</i>",
        ]
    )
    return "\n".join(lines)


def build_test_alert_message(config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    return "\n".join(
        [
            "🧪 <b>TEST ALERT — not a real slot</b>",
            "",
            "🚨🚨 <b>JAPAN VISA SLOT OPEN</b> 🚨🚨",
            "",
            f"📋 {label}",
            "",
            "🗓 <b>Newly available — book now:</b>",
            "   ✅ <b>2099-01-01</b> — TEST ONLY",
            "",
            f"⚡️ <a href=\"{escaped_url}\"><b>OPEN BOOKING CALENDAR</b></a>",
            "",
            "🧪 <i>This is only a delivery test.</i>",
            f"🕒 <i>{timestamp}</i>",
        ]
    )


def build_status_message(current: dict[str, str], config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    lines = [
        "<b>Japan visa slot status</b>",
        label,
    ]
    if current:
        lines.append("")
        lines.append("<b>Open dates</b>")
        for date_key, alt in sorted(current.items()):
            lines.append(f"- <b>{html.escape(date_key)}</b>: {html.escape(alt)}")
    else:
        lines.append("")
        lines.append("No open dates found right now.")
    lines.extend(
        [
            "",
            f"Book: <a href=\"{escaped_url}\">reservation calendar</a>",
            f"Checked: {timestamp} UTC",
        ]
    )
    return "\n".join(lines)


def build_help_message(current: dict[str, str], config: dict[str, Any]) -> str:
    return (
        "Send /status to check current Japan visa slots.\n"
        "Send /subscribe to receive newly opened slot alerts.\n"
        "Send /testalert to verify Telegram alert delivery.\n"
        "Send /unsubscribe to stop alerts.\n\n"
        + build_status_message(current, config)
    )


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def should_send_status(last_status_at: str | None, interval_seconds: int, now: datetime) -> bool:
    previous = parse_utc_timestamp(last_status_at)
    if previous is None:
        return True
    return (now - previous).total_seconds() >= interval_seconds


# Small delay between subscriber sends to stay under Telegram's broadcast rate.
INTER_SEND_DELAY = 0.1


class AlertResult:
    """Outcome of a broadcast: which chats failed and which are permanently dead."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.failed: set[str] = set()
        self.terminal: set[str] = set()
        self.dry_run_failed = False


def send_to_alert_targets(
    message: str,
    config: dict[str, Any],
    alert_targets: set[str],
) -> AlertResult:
    """Send ``message`` to every target.

    Returns an AlertResult capturing per-chat errors, the set of chats that
    failed, and the subset that failed permanently (403 blocked / 400
    chat-not-found) and should be removed from the subscriber list. Never raises
    on a per-chat send failure.
    """
    result = AlertResult()
    if not config["telegram_bot_token"] or not alert_targets:
        try:
            send_telegram(message, config)
        except Exception as exc:
            result.errors.append(str(exc))
            result.dry_run_failed = True
        return result

    targets = sorted(alert_targets)
    for index, chat_id in enumerate(targets):
        try:
            send_telegram_to_chat(config["telegram_bot_token"], chat_id, message)
        except Exception as exc:
            result.errors.append(f"chat {chat_id}: {exc}")
            result.failed.add(chat_id)
            if telegram_is_terminal(exc):
                result.terminal.add(chat_id)
        if index < len(targets) - 1:
            time.sleep(INTER_SEND_DELAY)
    return result


def handle_telegram_message(
    message: dict[str, Any],
    current: dict[str, str],
    config: dict[str, Any],
    subscribed_chats: set[str],
) -> tuple[set[str], bool, bool, str | None]:
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return subscribed_chats, False, False, None
    chat_id = str(chat["id"])
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return subscribed_chats, False, False, None

    command = command_from_text(text)
    reply: str | None = None
    if command in {"/start", "/help"}:
        reply = build_help_message(current, config)
    elif command == "/status":
        reply = build_status_message(current, config)
    elif command == "/subscribe":
        subscribed_chats.add(chat_id)
        reply = "Subscribed. You will receive newly opened Japan visa slot alerts.\n\n"
        reply += build_status_message(current, config)
    elif command == "/testalert":
        reply = build_test_alert_message(config)
    elif command == "/unsubscribe":
        subscribed_chats.discard(chat_id)
        reply = "Unsubscribed. Send /subscribe any time to receive alerts again."
    elif command.startswith("/"):
        reply = "Supported commands: /status, /subscribe, /testalert, /unsubscribe"
    else:
        return subscribed_chats, False, False, None

    try:
        send_telegram_to_chat(config["telegram_bot_token"], chat_id, reply)
    except Exception as exc:
        return subscribed_chats, True, False, f"reply failed: {exc}"
    return subscribed_chats, True, True, None


class TelegramError(RuntimeError):
    """Telegram API error carrying the HTTP status code when available."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def telegram_is_terminal(exc: Exception) -> bool:
    """403 (blocked/kicked) and 400 (chat not found) are permanent for a chat."""
    return isinstance(exc, TelegramError) and exc.status_code in {400, 403}


def _telegram_request_once(token: str, method: str, params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retry_after: int | None = None
        try:
            parsed = json.loads(body)
            detail = parsed.get("description", body)
            params_field = parsed.get("parameters")
            if isinstance(params_field, dict):
                raw_retry = params_field.get("retry_after")
                if isinstance(raw_retry, int):
                    retry_after = raw_retry
        except json.JSONDecodeError:
            detail = body or HTTPStatus(exc.code).phrase
        err = TelegramError(f"Telegram {method} failed: {detail}", status_code=exc.code)
        err.retry_after = retry_after  # type: ignore[attr-defined]
        raise err from exc

    if not payload.get("ok"):
        raise TelegramError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
    return payload


def telegram_request(token: str, method: str, params: dict[str, str]) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return _telegram_request_once(token, method, params)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= RETRY_ATTEMPTS:
                raise
            status = getattr(exc, "status_code", None)
            if status == 429:
                retry_after = getattr(exc, "retry_after", None)
                delay = float(retry_after) if isinstance(retry_after, int) else RETRY_BASE_DELAY * (2 ** (attempt - 1))
                delay = min(delay, RETRY_MAX_DELAY)
                print(
                    f"[warn] Telegram {method} rate-limited (attempt {attempt}/{RETRY_ATTEMPTS}); "
                    f"retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            if not _is_transient(exc):
                raise
            delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
            print(
                f"[warn] Telegram {method} transient failure (attempt {attempt}/{RETRY_ATTEMPTS}): {exc}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def send_telegram_to_chat(token: str, chat_id: str, message: str) -> None:
    telegram_request(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )


def send_telegram(message: str, config: dict[str, Any]) -> None:
    token = config["telegram_bot_token"]
    chat_ids = config["telegram_chat_ids"]
    if not token or not chat_ids:
        print("[dry-run] Telegram credentials missing; message would be:")
        print(message)
        return
    for chat_id in chat_ids:
        send_telegram_to_chat(token, str(chat_id), message)


def get_telegram_updates(token: str, offset: int | None) -> list[dict[str, Any]]:
    params = {
        "timeout": "0",
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = str(offset)
    try:
        payload = telegram_request(token, "getUpdates", params)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "webhook" not in message or "getupdates" not in message:
            raise
        telegram_request(token, "deleteWebhook", {"drop_pending_updates": "false"})
        print("[telegram] deleted stale webhook; retrying getUpdates", flush=True)
        payload = telegram_request(token, "getUpdates", params)
    result = payload.get("result", [])
    return result if isinstance(result, list) else []


def command_from_text(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0].lower()
    return first.split("@", maxsplit=1)[0]


def process_telegram_commands(
    current: dict[str, str],
    config: dict[str, Any],
    offset: int | None,
    subscribed_chats: set[str],
) -> tuple[int | None, set[str], list[str], dict[str, int]]:
    stats = {
        "updates": 0,
        "commands": 0,
        "replies": 0,
    }
    token = config["telegram_bot_token"]
    if not token or config.get("telegram_webhook_secret"):
        return offset, subscribed_chats, [], stats

    errors: list[str] = []
    try:
        updates = get_telegram_updates(token, offset)
    except Exception as exc:
        return offset, subscribed_chats, [str(exc)], stats

    stats["updates"] = len(updates)
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")

        message = update.get("message")
        reply_failed = False
        if isinstance(message, dict):
            subscribed_chats, command_seen, reply_sent, error = handle_telegram_message(
                message,
                current,
                config,
                subscribed_chats,
            )
            if command_seen:
                stats["commands"] += 1
            if reply_sent:
                stats["replies"] += 1
            if error:
                errors.append(error)
                # A command was recognized but its reply failed to send. Do NOT
                # advance the offset past it so the read-only command is retried
                # next cycle instead of being silently swallowed.
                if command_seen and not reply_sent:
                    reply_failed = True

        if reply_failed:
            break
        if isinstance(update_id, int):
            next_offset = update_id + 1

    return next_offset, subscribed_chats, errors, stats


def persist_telegram_metadata(
    config: dict[str, Any],
    path: Path,
    telegram_update_offset: int | None,
    subscribed_chats: set[str],
    last_status_at: str | None,
    previous_chats: set[str] | None = None,
) -> None:
    """Persist telegram sub-state without clobbering a concurrent writer.

    ``previous_chats`` is the subscriber snapshot this caller started from; the
    delta against ``subscribed_chats`` is applied atomically (Upstash SET ops) or
    merged onto a fresh read (Supabase/local) so a slow scan cannot revert a
    subscribe/unsubscribe that happened meanwhile.
    """
    previous = set(previous_chats) if previous_chats is not None else set(subscribed_chats)
    added = subscribed_chats - previous
    removed = previous - subscribed_chats

    if upstash_enabled(config):
        # Seed the SET from the blob on first migration if it is empty.
        existing = upstash_read_subscribers(config)
        if not existing and previous:
            for chat_id in sorted(previous):
                upstash_add_subscriber(config, chat_id)
        apply_subscriber_delta(config, added, removed)
        merged = (upstash_read_subscribers(config) | added) - removed
        upsert_upstash_value(
            config,
            telegram_state_key(config),
            telegram_state_value(telegram_update_offset, merged, last_status_at),
        )
        return

    if supabase_enabled(config):
        fresh = read_supabase_state(config)
        merged = (set(fresh["subscribed_chats"]) | added) - removed
        merged_offset = telegram_update_offset
        if isinstance(fresh["telegram_update_offset"], int):
            merged_offset = max(telegram_update_offset or 0, fresh["telegram_update_offset"])
        upsert_supabase_value(
            config,
            telegram_state_key(config),
            telegram_state_value(merged_offset, merged, last_status_at),
        )
        return

    # Local file: re-read immediately before writing and merge the delta.
    state = read_state(path, config)
    merged = (set(state["subscribed_chats"]) | added) - removed
    save_state(
        path,
        state["open_day_alts"],
        telegram_update_offset,
        merged,
        config,
        last_status_at,
    )


def save_telegram_metadata(
    path: Path,
    config: dict[str, Any],
    telegram_update_offset: int | None,
    subscribed_chats: set[str],
    last_status_at: str | None,
    previous_chats: set[str] | None = None,
) -> None:
    persist_telegram_metadata(
        config,
        path,
        telegram_update_offset,
        subscribed_chats,
        last_status_at,
        previous_chats,
    )


def poll_telegram_commands_once(current: dict[str, str], config: dict[str, Any]) -> None:
    state = read_state(config["state_file"], config)
    previous_chats = set(state["subscribed_chats"])
    telegram_update_offset, subscribed_chats, command_errors, command_stats = process_telegram_commands(
        current,
        config,
        state["telegram_update_offset"],
        set(previous_chats),
    )
    if command_stats["updates"] or command_stats["commands"] or command_stats["replies"]:
        print(
            "[telegram-fast] "
            f"updates={command_stats['updates']} "
            f"commands={command_stats['commands']} "
            f"replies={command_stats['replies']}",
            flush=True,
        )
    for error in command_errors:
        print(f"[error] Telegram command failed: {error}", file=sys.stderr)

    save_telegram_metadata(
        config["state_file"],
        config,
        telegram_update_offset,
        subscribed_chats,
        state["last_status_at"],
        previous_chats,
    )


def current_from_state(state: dict[str, Any]) -> dict[str, str]:
    alts = state.get("open_day_alts") or {}
    result: dict[str, str] = {}
    for date_key in sorted(state["open_dates"]):
        key = str(date_key)
        alt = str(alts.get(key) or "").strip()
        # Surface the real stored slot status; fall back only when absent so
        # /status shows the true alt text instead of a fabricated "Available".
        result[key] = alt or "Available"
    return result


def handle_telegram_webhook_update(update: dict[str, Any]) -> dict[str, int]:
    config = get_config()
    if not config["telegram_bot_token"]:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram webhook replies")

    state = read_state(config["state_file"], config)
    current = current_from_state(state)
    stats = {"commands": 0, "replies": 0, "skipped": 0}

    # update_id de-duplication: Telegram redelivers updates for ~24h if it does
    # not receive a prompt 200. Use telegram_update_offset as a high-water mark
    # and skip any update we have already processed.
    update_id = update.get("update_id")
    high_water = state["telegram_update_offset"]
    if isinstance(update_id, int) and isinstance(high_water, int) and update_id < high_water:
        stats["skipped"] = 1
        return stats

    previous_chats = set(state["subscribed_chats"])
    subscribed_chats = set(previous_chats)
    message = update.get("message")
    error: str | None = None
    if isinstance(message, dict):
        subscribed_chats, command_seen, reply_sent, error = handle_telegram_message(
            message,
            current,
            config,
            subscribed_chats,
        )
        if command_seen:
            stats["commands"] += 1
        if reply_sent:
            stats["replies"] += 1

    # Advance the high-water mark to update_id + 1 (so this id is not reprocessed).
    next_offset = state["telegram_update_offset"]
    if isinstance(update_id, int):
        next_offset = update_id + 1

    # persist_telegram_metadata re-reads/merges immediately before writing so a
    # concurrent writer's subscriber change is not clobbered.
    save_telegram_metadata(
        config["state_file"],
        config,
        next_offset,
        subscribed_chats,
        state["last_status_at"],
        previous_chats,
    )
    if error:
        raise RuntimeError(error)
    return stats


def owner_alert_succeeded(config: dict[str, Any], result: AlertResult) -> bool:
    """Did the alert reach a fixed recipient (configured TELEGRAM_CHAT_ID)?

    Delivery to a fixed recipient is the gate for recording a newly-opened date
    as known. With several fixed recipients, succeeding for at least one is
    enough to mark the date seen — otherwise one user who blocked the bot would
    make every cycle re-alert everyone forever. When no fixed recipient is
    configured the send goes through the dry-run path; treat that as success
    unless the dry-run itself failed.
    """
    fixed = [str(c) for c in config["telegram_chat_ids"]]
    if not fixed or not config["telegram_bot_token"]:
        return not result.dry_run_failed
    return any(chat_id not in result.failed for chat_id in fixed)


def cycle() -> dict[str, str]:
    config = get_config()
    warn_if_ephemeral_state(config)
    current = run_once()
    current_dates = set(current)
    state = read_state(config["state_file"], config)
    previous_dates = set(state["open_dates"])
    previous_alts = dict(state.get("open_day_alts") or {})
    telegram_update_offset = state["telegram_update_offset"]
    previous_chats = set(state["subscribed_chats"])
    subscribed_chats = set(previous_chats)
    last_status_at = state["last_status_at"]
    new_dates = sorted(current_dates - previous_dates)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    send_errors: list[str] = []
    terminal_chats: set[str] = set()

    # Baseline of known open-dates we will persist. Start from dates that were
    # already known previously AND are still open (they stay known). Newly-opened
    # dates are added only after the owner alert for them succeeds (fix 2).
    persisted_alts: dict[str, str] = {
        date_key: str(current.get(date_key, previous_alts.get(date_key, "")))
        for date_key in (previous_dates & current_dates)
    }

    try:
        telegram_update_offset, subscribed_chats, command_errors, command_stats = process_telegram_commands(
            current,
            config,
            telegram_update_offset,
            subscribed_chats,
        )
        print(
            "[telegram] "
            f"updates={command_stats['updates']} "
            f"commands={command_stats['commands']} "
            f"replies={command_stats['replies']}",
            flush=True,
        )
        for error in command_errors:
            send_errors.append(error)
            print(f"[error] Telegram command failed: {error}", file=sys.stderr)

        alert_targets = set(subscribed_chats)
        for chat_id in config["telegram_chat_ids"]:
            alert_targets.add(str(chat_id))

        if new_dates:
            message = build_alert_message(new_dates, current, config)
            result = send_to_alert_targets(message, config, alert_targets)
            terminal_chats |= result.terminal
            for error in result.errors:
                send_errors.append(f"alert: {error}")
                print(f"[error] Telegram alert send failed: {error}", file=sys.stderr)
            # Only record the newly-opened dates as known once the OWNER alert
            # actually succeeded; otherwise leave them out so they retry next
            # cycle instead of being silently lost.
            if owner_alert_succeeded(config, result):
                for date_key in new_dates:
                    persisted_alts[date_key] = str(current[date_key])
                last_status_at = now_iso
        elif should_send_status(last_status_at, int(config["status_interval_seconds"]), now):
            result = send_to_alert_targets(build_status_message(current, config), config, alert_targets)
            terminal_chats |= result.terminal
            for error in result.errors:
                send_errors.append(f"status: {error}")
                print(f"[error] Telegram status send failed: {error}", file=sys.stderr)
            # last_status_at advances on ATTEMPT so one dead chat cannot trigger
            # minute-by-minute re-broadcasts to everyone else (fix 3).
            last_status_at = now_iso

        # Prune permanently-dead chats so they are not retried forever (fix 3).
        if terminal_chats:
            subscribed_chats -= terminal_chats
            for chat_id in sorted(terminal_chats):
                print(f"[telegram] dropping dead subscriber {chat_id}", file=sys.stderr)
    finally:
        # Open-dates state is owned by this scan; persist it directly. Telegram
        # sub-state is merged against a fresh read so we never clobber a
        # concurrent subscribe/unsubscribe (fix 17).
        save_state(config["state_file"], persisted_alts, telegram_update_offset, previous_chats, config, last_status_at)
        save_telegram_metadata(
            config["state_file"],
            config,
            telegram_update_offset,
            subscribed_chats,
            last_status_at,
            previous_chats,
        )

    print(
        f"[summary] open={len(current_dates)} new={len(new_dates)} "
        f"subscribers={len(subscribed_chats)} status_at={last_status_at or 'never'} state={state_label(config)}",
        flush=True,
    )
    if send_errors:
        # Command-handling errors are surfaced for visibility, but per-chat send
        # failures must NOT crash the cycle (fix 3). Only raise when something
        # other than a tolerated per-chat send failure occurred.
        unexpected = [
            err for err in send_errors
            if not (err.startswith("alert: chat ") or err.startswith("status: chat "))
        ]
        if unexpected:
            raise RuntimeError("; ".join(unexpected))
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Japan visa reservation slots.")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Print AJAX/session diagnostics and test month-navigation parameters.",
    )
    parser.add_argument(
        "--loop",
        type=int,
        metavar="SECONDS",
        help="Run continuously and sleep SECONDS between checks.",
    )
    parser.add_argument(
        "--telegram-poll-interval",
        type=int,
        default=env_int("TELEGRAM_POLL_INTERVAL_SECONDS", 5),
        metavar="SECONDS",
        help="Poll Telegram commands this often between slot checks.",
    )
    args = parser.parse_args()

    if args.probe:
        run_probe()
        return 0

    if args.loop is None:
        cycle()
        return 0

    if args.loop <= 0:
        parser.error("--loop must be a positive number of seconds")
    if args.telegram_poll_interval <= 0:
        parser.error("--telegram-poll-interval must be a positive number of seconds")

    current: dict[str, str] = {}
    while True:
        try:
            current = cycle()
        except Exception as exc:
            print(f"[error] {datetime.now(timezone.utc).isoformat(timespec='seconds')} {exc}", file=sys.stderr)

        deadline = time.monotonic() + args.loop
        while time.monotonic() < deadline:
            time.sleep(min(args.telegram_poll_interval, max(0.0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            try:
                poll_telegram_commands_once(current, get_config())
            except Exception as exc:
                print(
                    f"[error] {datetime.now(timezone.utc).isoformat(timespec='seconds')} telegram poll: {exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
