import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from check_slots import cycle, get_config, handle_telegram_webhook_update, state_label, supabase_enabled, upstash_enabled


def header_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("latin1")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin1")
    return None


async def read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def handle_check(scope, send) -> None:
    secret = os.environ.get("CRON_SECRET")
    auth = header_value(scope.get("headers", []), "authorization")
    if secret and auth != f"Bearer {secret}":
        await send_json(send, 401, {"error": "unauthorized"})
        return

    try:
        current = cycle()
        await send_json(send, 200, {"ok": True, "open": sorted(current)})
    except Exception as exc:
        await send_json(send, 500, {"ok": False, "error": str(exc)})


async def handle_telegram(scope, receive, send) -> None:
    if scope.get("method") == "GET":
        await send_json(send, 200, {"ok": True})
        return
    if scope.get("method") != "POST":
        await send_json(send, 405, {"ok": False, "error": "method not allowed"})
        return

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    token = header_value(scope.get("headers", []), "x-telegram-bot-api-secret-token")
    if secret and token != secret:
        await send_json(send, 401, {"error": "unauthorized"})
        return

    try:
        update = json.loads((await read_body(receive)).decode("utf-8"))
        stats = handle_telegram_webhook_update(update)
        await send_json(send, 200, {"ok": True, **stats})
    except Exception as exc:
        await send_json(send, 500, {"ok": False, "error": str(exc)})


async def handle_health(send) -> None:
    config = get_config()
    await send_json(
        send,
        200,
        {
            "ok": True,
            "event_id": config["event_id"],
            "category_id": config["category_id"],
            "plan_id": config["plan_id"],
            "months_ahead": config["months_ahead"],
            "status_interval_seconds": config["status_interval_seconds"],
            "telegram_bot_configured": bool(config["telegram_bot_token"]),
            "telegram_webhook_configured": bool(config["telegram_webhook_secret"]),
            "telegram_default_chat_configured": bool(config["telegram_chat_id"]),
            "upstash_configured": upstash_enabled(config),
            "supabase_configured": supabase_enabled(config),
            "state_backend": state_label(config),
            "cron_secret_configured": bool(os.environ.get("CRON_SECRET")),
        },
    )


async def app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        await send_json(send, 404, {"ok": False, "error": "not found"})
        return

    path = scope.get("path") or ""
    if path in {"/api/check", "/check"}:
        await handle_check(scope, send)
    elif path in {"/api/telegram", "/telegram"}:
        await handle_telegram(scope, receive, send)
    elif path in {"/api/health", "/health"}:
        await handle_health(send)
    else:
        await send_json(send, 404, {"ok": False, "error": "not found"})
