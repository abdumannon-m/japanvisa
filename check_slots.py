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
    "qabul tugadi",
    "qabul yakunlandi",
    "приём окончен",
    "прием окончен",
    "受付終了",
    "受付は終了",
]
POS = [
    "qabul qilinmoqda",
    "ведётся",
    "ведется",
    "受付中",
    "残りわずか",
    "few",
    "available",
]


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def get_config() -> dict[str, Any]:
    event_id = env_str("EVENT_ID", "20")
    return {
        "event_id": event_id,
        "category_id": env_str("CATEGORY_ID", "12"),
        "plan_id": env_str("PLAN_ID", "19"),
        "months_ahead": env_int("MONTHS_AHEAD", 2),
        "event_label": env_str("EVENT_LABEL", "Short stay - Applicant"),
        "month_param": env_str("MONTH_PARAM", "date"),
        "status_interval_seconds": env_int("STATUS_INTERVAL_SECONDS", 3600),
        "state_file": Path(env_str("STATE_FILE", "state.json")),
        "state_key": env_str("STATE_KEY", f"event-{event_id}"),
        "supabase_url": env_str("SUPABASE_URL").rstrip("/"),
        "supabase_service_key": env_str("SUPABASE_SERVICE_KEY"),
        "upstash_redis_rest_url": env_str("UPSTASH_REDIS_REST_URL").rstrip("/"),
        "upstash_redis_rest_token": env_str("UPSTASH_REDIS_REST_TOKEN"),
        "telegram_bot_token": env_str("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": env_str("TELEGRAM_CHAT_ID"),
        "telegram_webhook_secret": env_str("TELEGRAM_WEBHOOK_SECRET"),
    }


def is_open(alt: str | None) -> bool:
    text = (alt or "").lower()
    if any(part in text for part in NEG):
        return False
    return any(part in text for part in POS)


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


def empty_state() -> dict[str, Any]:
    return {
        "open_dates": set(),
        "telegram_update_offset": None,
        "subscribed_chats": set(),
        "last_status_at": None,
    }


def normalize_open_days(open_days: set[str] | dict[str, str]) -> dict[str, str]:
    if isinstance(open_days, dict):
        return {str(key): str(value) for key, value in open_days.items()}
    return {str(date_key): "" for date_key in open_days}


def apply_open_days(state: dict[str, Any], open_days: Any) -> None:
    if isinstance(open_days, dict):
        state["open_dates"] = {str(item) for item in open_days}
    elif isinstance(open_days, list):
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
    response = requests.post(
        str(config["upstash_redis_rest_url"]),
        headers=upstash_headers(config),
        data=json.dumps(list(args)),
        timeout=30,
    )
    response.raise_for_status()
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


def read_upstash_state(config: dict[str, Any]) -> dict[str, Any]:
    state = empty_state()
    apply_open_days(state, read_upstash_value(config, str(config["state_key"])))
    apply_telegram_state(state, read_upstash_value(config, telegram_state_key(config)))
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
        {
            "telegram_update_offset": telegram_update_offset,
            "subscribed_chats": sorted(subscribed_chats or set()),
            "last_status_at": last_status_at,
        },
    )


def read_supabase_value(config: dict[str, Any], key: str) -> Any:
    requests = import_requests()
    response = requests.get(
        supabase_state_url(config),
        headers=supabase_headers(config),
        params={"key": f"eq.{key}", "select": "value"},
        timeout=30,
    )
    response.raise_for_status()
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
    response = requests.post(
        supabase_state_url(config),
        headers=headers,
        params={"on_conflict": "key"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


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
        {
            "telegram_update_offset": telegram_update_offset,
            "subscribed_chats": sorted(subscribed_chats or set()),
            "last_status_at": last_status_at,
        },
    )


def state_label(config: dict[str, Any]) -> str:
    if upstash_enabled(config):
        return f"upstash:{config['state_key']}"
    if supabase_enabled(config):
        return f"supabase:{config['state_key']}"
    return str(config["state_file"])


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
        return state
    if not isinstance(data, dict):
        return state

    raw_dates = data.get("open_dates", [])
    if isinstance(raw_dates, list):
        state["open_dates"] = {str(item) for item in raw_dates}

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

    open_dates = set(open_days) if isinstance(open_days, dict) else set(open_days)
    payload = {
        "open_dates": sorted(open_dates),
        "subscribed_chats": sorted(subscribed_chats or set()),
        "telegram_update_offset": telegram_update_offset,
        "last_status_at": last_status_at,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
            self.cells.append({"parts": [], "alts": []})
            self.stack.append((tag, len(self.cells) - 1))
            return
        if tag == "img" and self.stack:
            alt = attr_map.get("alt")
            src = attr_map.get("src") or ""
            if alt and "icon_disabled" not in src:
                self.cells[self.stack[-1][1]]["alts"].append(alt)

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
        for alt in cell["alts"]:
            items.append({"day": day, "alt": alt})
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


def new_calendar_session(config: dict[str, Any]) -> Any:
    requests = import_requests()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    response = session.get(configured_booking_url(config), timeout=30)
    response.raise_for_status()
    if not session.cookies.get("USERSESSID") or not session.cookies.get("csrfToken"):
        raise RuntimeError("Initial calendar GET did not set required USERSESSID/csrfToken cookies")
    return session


def fetch_calendar_html(
    session: Any,
    config: dict[str, Any],
    target_month: str | None = None,
) -> tuple[str, str]:
    csrf_token = session.cookies.get("csrfToken")
    if not csrf_token:
        raise RuntimeError("Missing csrfToken cookie in calendar session")

    data = {
        "category": str(config["category_id"]),
        "event": str(config["event_id"]),
        "plan": str(config["plan_id"]),
        "disp_type": "month",
        "_csrfToken": csrf_token,
        "search": "exec",
    }
    if target_month:
        data[str(config["month_param"])] = month_param_value(target_month)

    response = session.post(AJAX_URL, headers=ajax_headers(config), data=data, timeout=30)
    response.raise_for_status()
    return extract_html(response.text), response.text


def redacted_cookies(session: Any) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in session.cookies.items():
        cookies[str(name)] = f"<present len={len(str(value))}>"
    return cookies


def candidate_month_values(month: str) -> list[str]:
    year, month_number = month.split("-", maxsplit=1)
    return [
        f"{year}{month_number}",
        f"{year}-{month_number}",
        f"{year}/{month_number}/01",
        f"{year}/{month_number}/25",
        f"{year}-{month_number}-01",
        f"{year}-{month_number}-25",
    ]


def probe_month_params(session: Any, config: dict[str, Any], current_month: str) -> list[tuple[str, str, str]]:
    csrf_token = session.cookies.get("csrfToken")
    if not csrf_token:
        raise RuntimeError("Missing csrfToken cookie in calendar session")

    next_month = add_months(current_month, 1)
    advancing: list[tuple[str, str, str]] = []
    for name in ["ym", "date", "target_date", "month", "targetYm"]:
        for value in candidate_month_values(next_month):
            data = {
                "category": str(config["category_id"]),
                "_csrfToken": csrf_token,
                "search": "exec",
                name: value,
            }
            response = session.post(AJAX_URL, headers=ajax_headers(config), data=data, timeout=30)
            response.raise_for_status()
            month = parse_month_from_html(extract_html(response.text))
            marker = "ADVANCES" if month != current_month else "same"
            print(f"[probe] candidate {name}={value} -> {month} ({marker})", flush=True)
            if month != current_month:
                advancing.append((name, value, month))
    return advancing


def run_probe() -> None:
    config = get_config()
    session = new_calendar_session(config)
    print(f"[probe] GET {configured_booking_url(config)}")
    print(f"[probe] cookies={json.dumps(redacted_cookies(session), sort_keys=True)}")

    html_doc, raw_response = fetch_calendar_html(session, config)
    month = parse_month_from_html(html_doc)
    scan = scan_calendar_html(html_doc)
    print(f"[probe] parsed_month={month} icons={len(scan['items'])}")
    print("[probe] raw AJAX response follows")
    print(raw_response)

    advancing = probe_month_params(session, config, month)
    if advancing:
        details = ", ".join(f"{name}={value}->{advanced_month}" for name, value, advanced_month in advancing)
        print(f"[probe] advancing candidates: {details}")
    else:
        print("[probe] no candidate advanced the calendar month")


def log_month(month: str, scan: dict[str, Any], current_open: dict[str, str]) -> None:
    open_dates: list[str] = []
    icon_count = 0
    for item in scan["items"]:
        icon_count += 1
        day = item.get("day")
        alt = item.get("alt") or ""
        if not day or not is_open(str(alt)):
            continue
        date_key = f"{month}-{int(str(day)):02d}"
        open_dates.append(date_key)
        current_open[date_key] = str(alt)

    unique_open = sorted(set(open_dates))
    open_log = ",".join(unique_open) if unique_open else "none"
    print(f"[{month}] icons={icon_count} open={open_log}", flush=True)


def run_once() -> dict[str, str]:
    config = get_config()
    session = new_calendar_session(config)
    current_open: dict[str, str] = {}
    previous_month: str | None = None
    start_month: str | None = None

    for index in range(config["months_ahead"] + 1):
        target_month = add_months(start_month, index) if start_month and index > 0 else None
        html_doc, _raw = fetch_calendar_html(session, config, target_month)
        month = parse_month_from_html(html_doc)
        if start_month is None:
            start_month = month
        if previous_month is not None and month == previous_month:
            print("[warn] month did not advance — MONTH_PARAM wrong", file=sys.stderr)
            break

        scan = scan_calendar_html(html_doc)
        log_month(month, scan, current_open)
        previous_month = month

    return current_open


def build_message(date_key: str, alt: str, config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_date = html.escape(date_key)
    escaped_alt = html.escape(alt)
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    return (
        f"<b>Japan visa slot open</b>\n"
        f"{label}\n"
        f"Date: <b>{escaped_date}</b>\n"
        f"Status: {escaped_alt}\n"
        f"Book: <a href=\"{escaped_url}\">reservation calendar</a>\n"
        f"Checked: {timestamp} UTC"
    )


def build_alert_message(date_keys: list[str], current: dict[str, str], config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    lines = [
        "<b>Japan visa slots open</b>",
        label,
        "",
        "<b>Newly opened dates</b>",
    ]
    for date_key in date_keys:
        lines.append(f"- <b>{html.escape(date_key)}</b>: {html.escape(current[date_key])}")
    lines.extend(
        [
            "",
            f"Book: <a href=\"{escaped_url}\">reservation calendar</a>",
            f"Checked: {timestamp} UTC",
        ]
    )
    return "\n".join(lines)


def build_test_alert_message(config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(configured_booking_url(config), quote=True)
    return "\n".join(
        [
            "<b>Japan visa test alert</b>",
            label,
            "",
            "TEST ONLY: no real visa slot is being reported.",
            "",
            "<b>Simulated newly opened date</b>",
            "- <b>2099-01-01</b>: TEST ONLY",
            "",
            f"Book: <a href=\"{escaped_url}\">reservation calendar</a>",
            f"Checked: {timestamp} UTC",
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


def send_to_alert_targets(message: str, config: dict[str, Any], alert_targets: set[str]) -> list[str]:
    errors: list[str] = []
    if not config["telegram_bot_token"] or not alert_targets:
        try:
            send_telegram(message, config)
        except Exception as exc:
            errors.append(str(exc))
        return errors

    for chat_id in sorted(alert_targets):
        try:
            send_telegram_to_chat(config["telegram_bot_token"], chat_id, message)
        except Exception as exc:
            errors.append(str(exc))
    return errors


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


def telegram_request(token: str, method: str, params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(
        params
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("description", body)
        except json.JSONDecodeError:
            detail = body or HTTPStatus(exc.code).phrase
        raise RuntimeError(f"Telegram {method} failed: {detail}") from exc

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
    return payload


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
    chat_id = config["telegram_chat_id"]
    if not token or not chat_id:
        print("[dry-run] Telegram credentials missing; message would be:")
        print(message)
        return
    send_telegram_to_chat(token, chat_id, message)


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
        if isinstance(update_id, int):
            next_offset = update_id + 1

        message = update.get("message")
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

    return next_offset, subscribed_chats, errors, stats


def save_telegram_metadata(
    path: Path,
    config: dict[str, Any],
    telegram_update_offset: int | None,
    subscribed_chats: set[str],
    last_status_at: str | None,
) -> None:
    if remote_state_enabled(config):
        value = {
            "telegram_update_offset": telegram_update_offset,
            "subscribed_chats": sorted(subscribed_chats),
            "last_status_at": last_status_at,
        }
        if upstash_enabled(config):
            upsert_upstash_value(config, telegram_state_key(config), value)
        else:
            upsert_supabase_value(config, telegram_state_key(config), value)
        return

    state = read_state(path, config)
    save_state(
        path,
        set(state["open_dates"]),
        telegram_update_offset,
        subscribed_chats,
        config,
        last_status_at,
    )


def poll_telegram_commands_once(current: dict[str, str], config: dict[str, Any]) -> None:
    state = read_state(config["state_file"], config)
    telegram_update_offset, subscribed_chats, command_errors, command_stats = process_telegram_commands(
        current,
        config,
        state["telegram_update_offset"],
        set(state["subscribed_chats"]),
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
    )


def handle_telegram_webhook_update(update: dict[str, Any]) -> dict[str, int]:
    config = get_config()
    if not config["telegram_bot_token"]:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram webhook replies")

    current = run_once()
    state = read_state(config["state_file"], config)
    subscribed_chats = set(state["subscribed_chats"])
    message = update.get("message")
    stats = {"commands": 0, "replies": 0}
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
            raise RuntimeError(error)

    save_telegram_metadata(
        config["state_file"],
        config,
        state["telegram_update_offset"],
        subscribed_chats,
        state["last_status_at"],
    )
    return stats


def cycle() -> dict[str, str]:
    config = get_config()
    current = run_once()
    current_dates = set(current)
    state = read_state(config["state_file"], config)
    previous_dates = set(state["open_dates"])
    telegram_update_offset = state["telegram_update_offset"]
    subscribed_chats = set(state["subscribed_chats"])
    last_status_at = state["last_status_at"]
    new_dates = sorted(current_dates - previous_dates)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    send_errors: list[str] = []

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
        if config["telegram_chat_id"]:
            alert_targets.add(str(config["telegram_chat_id"]))

        if new_dates:
            message = build_alert_message(new_dates, current, config)
            alert_errors = send_to_alert_targets(message, config, alert_targets)
            if alert_errors:
                for error in alert_errors:
                    send_errors.append(f"alert: {error}")
                    print(f"[error] Telegram alert send failed: {error}", file=sys.stderr)
            else:
                last_status_at = now_iso
        elif should_send_status(last_status_at, int(config["status_interval_seconds"]), now):
            status_errors = send_to_alert_targets(build_status_message(current, config), config, alert_targets)
            if status_errors:
                for error in status_errors:
                    send_errors.append(f"status: {error}")
                    print(f"[error] Telegram status send failed: {error}", file=sys.stderr)
            else:
                last_status_at = now_iso
    finally:
        save_state(config["state_file"], current, telegram_update_offset, subscribed_chats, config, last_status_at)

    print(
        f"[summary] open={len(current_dates)} new={len(new_dates)} "
        f"subscribers={len(subscribed_chats)} status_at={last_status_at or 'never'} state={state_label(config)}",
        flush=True,
    )
    if send_errors:
        raise RuntimeError("; ".join(send_errors))
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
