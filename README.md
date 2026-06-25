# Japan Visa Slot Monitor

Telegram monitor for the Embassy of Japan in Uzbekistan reservation calendar. It watches the short-stay visa Applicant calendar through the embassy AJAX endpoint and sends a Telegram message when a newly opened appointment day appears.

This is a small, personal bot whose job is to notify you when a short-stay Japan visa interview slot opens. The bot is open to everyone: anyone who finds it on Telegram can send commands and `/subscribe` to receive newly opened slot alerts.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Optional: if you want a fixed default alert target, send a message to the bot, then get your chat id from:

   ```text
   https://api.telegram.org/bot<token>/getUpdates
   ```

   Use `result[].message.chat.id` as `TELEGRAM_CHAT_ID`.
3. Make this repository public. Public repositories have unlimited free GitHub Actions minutes; a frequent schedule like the `*/15` cron used here can exceed the private repository free tier.
4. Add repository secrets in GitHub under **Settings -> Secrets and variables -> Actions -> Secrets -> Repository secrets**. Use **New repository secret**, not environment secrets and not variables:

   ```text
   TELEGRAM_BOT_TOKEN
   ```

5. Optional: add `TELEGRAM_CHAT_ID` only if you want every newly opened slot alert sent to one default chat, group, or public channel. For a public channel, add the bot as an admin and use the channel username, for example `@your_channel_name`.
6. Run **Actions -> Visa Slot Monitor -> Run workflow** once to test.

## Bot Commands

Anyone who messages the bot can send:

```text
/start
/status
/subscribe
/testalert
/unsubscribe
```

`/status` returns the currently open Japan visa dates. `/subscribe` stores that chat in state so the bot sends newly opened slot alerts to that user or group. `/testalert` sends a clearly marked simulated alert only to the chat that requested it. Use it after `/subscribe` to prove Telegram alert delivery without waiting for a real slot.

Simple production mode uses GitHub Actions polling. It needs only `TELEGRAM_BOT_TOKEN`, checks slots every 60 seconds, polls Telegram commands every 5 seconds between slot checks, and sends hourly status updates or immediate new-slot alerts. If a stale Telegram webhook exists, polling mode deletes it automatically and retries. Note: when `TELEGRAM_WEBHOOK_SECRET` is configured, the GitHub Actions command poller is disabled and GitHub Actions only checks slots; Telegram commands are then answered by the Vercel `/api/telegram` webhook (see [Optional Vercel Pro](#optional-vercel-pro)).

## Local Use

Install dependencies:

```sh
python -m pip install -r requirements.txt
```

Run one check:

```sh
python check_slots.py
```

Run continuously, checking slots every 60 seconds and Telegram commands every 5 seconds:

```sh
python check_slots.py --loop 60 --telegram-poll-interval 5
```

If `TELEGRAM_BOT_TOKEN` is missing, the script cannot answer commands. Any chat that messages the bot is served. Set `TELEGRAM_CHAT_ID` if you want a fixed default chat to always receive alerts even without subscribing.

## Configuration

Environment variables:

```text
EVENT_ID=20
CATEGORY_ID=12
PLAN_ID=19
MONTHS_AHEAD=2
EVENT_LABEL=Short stay - Applicant
MONTH_PARAM=date
STATUS_INTERVAL_SECONDS=3600
STATE_FILE=state.json
STATE_KEY=event-20
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID= optional default alert target (chat, group, or channel)
TELEGRAM_WEBHOOK_SECRET= required for the Vercel /api/telegram webhook (fails closed without it)
TELEGRAM_POLL_INTERVAL_SECONDS=5
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
CRON_SECRET= required for the Vercel /api/check cron endpoint (fails closed without it)
```

`TELEGRAM_WEBHOOK_SECRET` and `CRON_SECRET` are required for the serverless endpoints and fail closed: the Vercel `/api/telegram` and `/api/check` endpoints reject requests when their secret is not configured, so the serverless functions are never exposed without authentication.

The default `EVENT_ID=20`, `CATEGORY_ID=12`, and `PLAN_ID=19` are the live values selected by the reservation site for `VISA Application for short stay (Applicant)`. The booking link includes both `event=20` and `category=12`; using only `event=20` currently opens the COE calendar. `MONTH_PARAM=date` is the discovered next-month request field and can be overridden if the embassy changes the endpoint.

## Probe Mode

Use probe mode to verify the plain-HTTP session, CSRF cookie, AJAX response, and month-navigation parameter:

```sh
python check_slots.py --probe
```

It prints the seeded cookie status, the raw AJAX response, parsed month/icon counts, and candidate month parameter results.

## Infrastructure

Recommended always-on host: **GCP free-tier e2-micro** in `us-west1`, `us-central1`, or `us-east1`, running:

```sh
python check_slots.py --loop 60 --telegram-poll-interval 5
```

as a `systemd` service. The monitor no longer needs Playwright or Chromium, so it has a small Python/requests footprint and is suitable for a free-tier VM.

GitHub Actions remains a useful backup host. The included workflow starts on the `*/15` schedule, then keeps checking slots every 60 seconds for about 5 hours and 50 minutes. When no `TELEGRAM_WEBHOOK_SECRET` is set it also polls Telegram commands every 5 seconds between slot checks; when the webhook secret is set, the command poller is disabled and GitHub Actions only checks slots while the Vercel webhook answers commands. The repository concurrency setting allows only one active monitor and one pending replacement run, so it behaves like a near-continuous backup loop instead of a one-shot cron. GitHub scheduling can still lag or skip under load.

## Optional Vercel Pro

The repo also includes a Vercel Python entrypoint at `api/index.py` with three optional routes:

- `/api/check`: cron endpoint, protected by `CRON_SECRET`. It checks slots every minute on Vercel Pro production deployments.
- `/api/telegram`: Telegram webhook endpoint, protected by `TELEGRAM_WEBHOOK_SECRET`. It answers `/status`, `/subscribe`, `/testalert`, and `/unsubscribe` immediately for any chat.
- `/api/health`: non-secret deployment health endpoint. It reports only non-sensitive configuration (event/category/plan IDs, months-ahead, status interval) and deliberately does not disclose which protective secrets or state backends are configured.

A durable state backend is required for serverless persistence. Vercel functions are stateless and the local `state.json` does not survive between invocations, so without a backend the bot cannot remember which dates were already open, who is subscribed, or the Telegram update offset, which leads to missed or repeated alerts. The recommended setup is **Upstash Redis** using `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`; Supabase is also supported. If you do not have a database available, use the GitHub Actions polling mode above (which can persist to `state.json` within a single run).

`CRON_SECRET` and `TELEGRAM_WEBHOOK_SECRET` are required and fail closed. The `/api/check` cron endpoint rejects any request without `Authorization: Bearer $CRON_SECRET`, and `/api/telegram` rejects any request without the matching `X-Telegram-Bot-Api-Secret-Token`, so neither serverless endpoint accepts unauthenticated traffic.

### Run only one runner

Run only one slot-checking runner at a time: either the Vercel cron (`/api/check`) or the GitHub Actions workflow, not both. Both runners share the same durable state (the `STATE_KEY` map plus Telegram offset). Running them together causes them to clobber each other's state and consume the same Telegram update offset, which can drop commands and produce duplicate or missed alerts. For a serverless setup, prefer Vercel cron for slot checks and the Vercel webhook for commands; disable the GitHub Actions schedule. For a no-database setup, use GitHub Actions only.

Runbook:

```bash
# one-time
npm i -g vercel
vercel login
vercel link                      # link repo to the Pro project

# create a free Upstash Redis database, then copy the REST URL and REST token
# from the database's REST API section in the Upstash console.

# prompts locally for Telegram/Upstash secrets, writes them to Vercel and GitHub,
# redeploys production, and registers the Telegram webhook
python scripts/bootstrap_vercel_upstash_webhook.py --url https://japanvisa-nine.vercel.app
```

Notes:

- Cron jobs run on production only.
- Changing the schedule requires a redeploy because cadence lives in `vercel.json`.
- When `TELEGRAM_WEBHOOK_SECRET` is set, the GitHub Actions command poller is disabled and command handling is done by `/api/telegram`. GitHub Actions, if still running, only checks slots.
- The Upstash REST token must be stored only as a server-side Vercel/GitHub secret. Do not put it in frontend code.
- Run only one slot-checking runner (see [Run only one runner](#run-only-one-runner)). If you adopt the Vercel cron, disable the GitHub Actions schedule so the two do not share and clobber the same state.
- If you do keep GitHub Actions running alongside the webhook, it must also receive `TELEGRAM_WEBHOOK_SECRET`; otherwise its `getUpdates` poller conflicts with the registered webhook. The bootstrap script handles this unless `--skip-github-secrets` is passed.

## State

Without Upstash or Supabase env vars, the monitor writes currently open dates, Telegram update offset, subscribed chat ids, and last status timestamp to `state.json`. With `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`, it writes the current `{date: alt}` map under `STATE_KEY` and Telegram command metadata under a derived key. Supabase is still supported through `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, but Upstash takes precedence when both are configured. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again. If no new slot appears, a status message is sent after `STATUS_INTERVAL_SECONDS`.

## Limitations

GitHub scheduled workflows can lag or skip under load. For second-level command speed, run `python check_slots.py --loop 60 --telegram-poll-interval 5` on a VPS. Scheduled workflows can pause after 60 days of repository inactivity.
