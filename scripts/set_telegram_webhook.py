#!/usr/bin/env python3
"""Register the deployed Vercel Telegram webhook."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def set_webhook(base_url: str, token: str, secret: str = "") -> dict:
    webhook_url = base_url.rstrip("/") + "/api/telegram"
    payload = {
        "url": webhook_url,
        "allowed_updates": json.dumps(["message"]),
    }
    if secret:
        payload["secret_token"] = secret

    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body or exc.reason
        raise RuntimeError(f"setWebhook failed with HTTP {exc.code}: {detail}") from exc

    if not result.get("ok"):
        raise RuntimeError(f"setWebhook failed: {result}")
    return {"ok": True, "url": webhook_url, "telegram": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Set Telegram webhook to the Vercel /api/telegram endpoint.")
    parser.add_argument("base_url", help="Production deployment URL, for example https://example.vercel.app")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN must be set in the shell running this script")

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    result = set_webhook(args.base_url, token, secret)
    print(json.dumps({"ok": True, "url": result["url"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
