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
from http import HTTPStatus
from pathlib import Path
from typing import Any


BASE = "https://uzembassyryouji.rsvsys.jp/reservations/calendar"
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


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def get_config() -> dict[str, Any]:
    return {
        "event_id": os.getenv("EVENT_ID", "20"),
        "months_ahead": env_int("MONTHS_AHEAD", 2),
        "event_label": os.getenv("EVENT_LABEL", "Short stay - Applicant"),
        "state_file": Path(os.getenv("STATE_FILE", "state.json")),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
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


def booking_url(event_id: str) -> str:
    return f"{BASE}?{urllib.parse.urlencode({'event': event_id})}"


def read_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {
        "open_dates": set(),
        "telegram_update_offset": None,
        "subscribed_chats": set(),
    }

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

    return state


def load_state(path: Path) -> set[str]:
    return set(read_state(path)["open_dates"])


def save_state(
    path: Path,
    open_dates: set[str],
    telegram_update_offset: int | None = None,
    subscribed_chats: set[str] | None = None,
) -> None:
    payload = {
        "open_dates": sorted(open_dates),
        "subscribed_chats": sorted(subscribed_chats or set()),
        "telegram_update_offset": telegram_update_offset,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scan_month(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const headerCandidates = [
            ...document.querySelectorAll('h1,h2,h3,.month,.calendar-title,.fc-toolbar-title,caption,th')
          ];
          const header = headerCandidates.find((node) => /\\d{4}\\s*年\\s*0?\\d{1,2}\\s*月/.test(node.textContent || ''));
          const bodyText = document.body ? document.body.innerText : '';
          const fallbackMatch = bodyText.match(/\\d{4}\\s*年\\s*0?\\d{1,2}\\s*月/);
          const text = header ? header.textContent : (fallbackMatch ? fallbackMatch[0] : bodyText.slice(0, 200));

          const items = [...document.querySelectorAll('img[alt]')].map((img) => {
            const alt = img.getAttribute('alt') || '';
            const cell = img.closest('td,li') || img.parentElement;
            const cellText = cell ? (cell.innerText || cell.textContent || '') : '';
            const dayMatch = cellText.match(/\\b(\\d{1,2})\\b/);
            const link = img.closest('a') || (cell ? cell.querySelector('a') : null);
            return {
              alt,
              day: dayMatch ? dayMatch[1] : null,
              href: link ? link.href : null,
            };
          });

          return {text, items};
        }
        """
    )


def click_next_month(page: Any, previous_month: str) -> bool:
    candidates = [
        lambda: page.get_by_text("次月").first.click(timeout=2000),
        lambda: page.locator("a:has-text('次月')").first.click(timeout=2000),
        lambda: page.locator("[class*=next]").first.click(timeout=2000),
    ]
    for click_candidate in candidates:
        try:
            click_candidate()
            page.wait_for_load_state("networkidle", timeout=10000)
            page.wait_for_timeout(1200)
            month_text = page.evaluate(
                """
                () => {
                  const text = document.body ? document.body.innerText : '';
                  const match = text.match(/\\d{4}\\s*年\\s*0?\\d{1,2}\\s*月/);
                  return match ? match[0] : '';
                }
                """
            )
            if month_text and parse_year_month(month_text) != previous_month:
                return True
        except Exception as exc:  # Playwright raises implementation-specific errors.
            print(f"[debug] next-month candidate failed: {exc}", file=sys.stderr)
    return False


def run_once() -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    config = get_config()
    url = booking_url(config["event_id"])
    current_open: dict[str, str] = {}
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 1700},
            locale="uz-UZ",
            timezone_id="Asia/Tashkent",
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1800)

        for index in range(config["months_ahead"] + 1):
            scan = scan_month(page)
            month = parse_year_month(scan["text"])
            open_dates: list[str] = []
            icon_count = 0
            for item in scan["items"]:
                icon_count += 1
                day = item.get("day")
                alt = item.get("alt") or ""
                if not day or not is_open(alt):
                    continue
                date_key = f"{month}-{int(day):02d}"
                open_dates.append(date_key)
                current_open[date_key] = alt

            unique_open = sorted(set(open_dates))
            open_log = ",".join(unique_open) if unique_open else "none"
            print(f"[month {month}] icons={icon_count} open={open_log}", flush=True)

            if index >= config["months_ahead"]:
                break
            if not click_next_month(page, month):
                print("[warn] Could not advance to next month; stopping scan", file=sys.stderr)
                break

        context.close()
        browser.close()

    return current_open


def build_message(date_key: str, alt: str, config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_date = html.escape(date_key)
    escaped_alt = html.escape(alt)
    escaped_url = html.escape(booking_url(str(config["event_id"])), quote=True)
    return (
        f"<b>Japan visa slot open</b>\n"
        f"{label}\n"
        f"Date: <b>{escaped_date}</b>\n"
        f"Status: {escaped_alt}\n"
        f"Book: <a href=\"{escaped_url}\">reservation calendar</a>\n"
        f"Checked: {timestamp} UTC"
    )


def build_status_message(current: dict[str, str], config: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = html.escape(str(config["event_label"]))
    escaped_url = html.escape(booking_url(str(config["event_id"])), quote=True)
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
        "Send /unsubscribe to stop alerts.\n\n"
        + build_status_message(current, config)
    )


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
) -> tuple[int | None, set[str], list[str]]:
    token = config["telegram_bot_token"]
    if not token:
        return offset, subscribed_chats, []

    errors: list[str] = []
    try:
        updates = get_telegram_updates(token, offset)
    except Exception as exc:
        return offset, subscribed_chats, [str(exc)]

    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = update_id + 1

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue

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
        elif command == "/unsubscribe":
            subscribed_chats.discard(chat_id)
            reply = "Unsubscribed. Send /subscribe any time to receive alerts again."
        elif command.startswith("/"):
            reply = "Supported commands: /status, /subscribe, /unsubscribe"

        if reply:
            try:
                send_telegram_to_chat(token, chat_id, reply)
            except Exception as exc:
                errors.append(f"reply failed: {exc}")

    return next_offset, subscribed_chats, errors


def cycle() -> dict[str, str]:
    config = get_config()
    current = run_once()
    current_dates = set(current)
    state = read_state(config["state_file"])
    previous_dates = set(state["open_dates"])
    telegram_update_offset = state["telegram_update_offset"]
    subscribed_chats = set(state["subscribed_chats"])
    new_dates = sorted(current_dates - previous_dates)
    send_errors: list[str] = []

    try:
        telegram_update_offset, subscribed_chats, command_errors = process_telegram_commands(
            current,
            config,
            telegram_update_offset,
            subscribed_chats,
        )
        for error in command_errors:
            send_errors.append(error)
            print(f"[error] Telegram command failed: {error}", file=sys.stderr)

        alert_targets = set(subscribed_chats)
        if config["telegram_chat_id"]:
            alert_targets.add(str(config["telegram_chat_id"]))

        for date_key in new_dates:
            message = build_message(date_key, current[date_key], config)
            if not config["telegram_bot_token"] or not alert_targets:
                send_telegram(message, config)
                continue
            for chat_id in sorted(alert_targets):
                try:
                    send_telegram_to_chat(config["telegram_bot_token"], chat_id, message)
                except Exception as exc:
                    send_errors.append(f"{date_key}: {exc}")
                    print(f"[error] Telegram send failed for {date_key}: {exc}", file=sys.stderr)
    finally:
        save_state(config["state_file"], current_dates, telegram_update_offset, subscribed_chats)

    print(
        f"[summary] open={len(current_dates)} new={len(new_dates)} "
        f"subscribers={len(subscribed_chats)} state={config['state_file']}",
        flush=True,
    )
    if send_errors:
        raise RuntimeError("; ".join(send_errors))
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Japan visa reservation slots.")
    parser.add_argument(
        "--loop",
        type=int,
        metavar="SECONDS",
        help="Run continuously and sleep SECONDS between checks.",
    )
    args = parser.parse_args()

    if args.loop is None:
        cycle()
        return 0

    if args.loop <= 0:
        parser.error("--loop must be a positive number of seconds")

    while True:
        try:
            cycle()
        except Exception as exc:
            print(f"[error] {datetime.now(timezone.utc).isoformat(timespec='seconds')} {exc}", file=sys.stderr)
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
