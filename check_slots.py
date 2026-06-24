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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] Could not read state file {path}: {exc}", file=sys.stderr)
        return set()

    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        raw_dates = data.get("open_dates", [])
        if isinstance(raw_dates, list):
            return {str(item) for item in raw_dates}
    return set()


def save_state(path: Path, open_dates: set[str]) -> None:
    payload = {
        "open_dates": sorted(open_dates),
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


def send_telegram(message: str, config: dict[str, Any]) -> None:
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]
    if not token or not chat_id:
        print("[dry-run] Telegram credentials missing; message would be:")
        print(message)
        return

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def cycle() -> dict[str, str]:
    config = get_config()
    current = run_once()
    current_dates = set(current)
    previous_dates = load_state(config["state_file"])
    new_dates = sorted(current_dates - previous_dates)
    send_errors: list[str] = []

    try:
        for date_key in new_dates:
            try:
                send_telegram(build_message(date_key, current[date_key], config), config)
            except Exception as exc:
                send_errors.append(f"{date_key}: {exc}")
                print(f"[error] Telegram send failed for {date_key}: {exc}", file=sys.stderr)
    finally:
        save_state(config["state_file"], current_dates)

    print(
        f"[summary] open={len(current_dates)} new={len(new_dates)} "
        f"state={config['state_file']}",
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
