import hmac
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
    secret = os.environ.get("CRON_SECRET", "").strip()
    # Fail CLOSED: refuse to run unauthenticated when the secret is not set.
    if not secret:
        print("[error] CRON_SECRET is not configured; refusing to run /check", file=sys.stderr)
        await send_json(send, 503, {"ok": False, "error": "service unavailable"})
        return
    auth = header_value(scope.get("headers", []), "authorization") or ""
    if not hmac.compare_digest(auth, f"Bearer {secret}"):
        await send_json(send, 401, {"error": "unauthorized"})
        return

    try:
        current = cycle()
        await send_json(send, 200, {"ok": True, "open": sorted(current)})
    except Exception as exc:
        # Do not reflect internal exception text to clients.
        print(f"[error] /check failed: {exc}", file=sys.stderr)
        await send_json(send, 500, {"ok": False, "error": "internal error"})


async def handle_telegram(scope, receive, send) -> None:
    if scope.get("method") == "GET":
        await send_json(send, 200, {"ok": True})
        return
    if scope.get("method") != "POST":
        await send_json(send, 405, {"ok": False, "error": "method not allowed"})
        return

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    # Fail CLOSED: refuse to run unauthenticated when the secret is not set.
    if not secret:
        print("[error] TELEGRAM_WEBHOOK_SECRET is not configured; refusing webhook", file=sys.stderr)
        await send_json(send, 503, {"ok": False, "error": "service unavailable"})
        return
    token = header_value(scope.get("headers", []), "x-telegram-bot-api-secret-token") or ""
    if not hmac.compare_digest(token, secret):
        await send_json(send, 401, {"error": "unauthorized"})
        return

    # Parse the body first; an unparseable body is a real 400.
    try:
        update = json.loads((await read_body(receive)).decode("utf-8"))
    except Exception as exc:
        print(f"[error] telegram webhook: unparseable body: {exc}", file=sys.stderr)
        await send_json(send, 400, {"ok": False, "error": "bad request"})
        return

    # ACK fast: always 200 once parsed, even if internal handling raised, so
    # Telegram does not redeliver the same update for ~24h. Log errors instead.
    stats: dict = {}
    try:
        stats = handle_telegram_webhook_update(update)
    except Exception as exc:
        print(f"[error] telegram webhook handling failed: {exc}", file=sys.stderr)
    await send_json(send, 200, {"ok": True, **stats})


async def handle_health(send) -> None:
    config = get_config()
    # Do not disclose which protective secrets / state backends are configured.
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
