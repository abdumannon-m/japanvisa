# Japan Visa Slot Monitor

Telegram monitor for the Embassy of Japan in Uzbekistan reservation calendar. It watches the short-stay visa Applicant calendar through the embassy AJAX endpoint, answers public `/status` requests, and sends subscribed users a message when a newly opened appointment day appears.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Optional: if you want a fixed default alert target, send a message to the bot, then get your chat id from:

   ```text
   https://api.telegram.org/bot<token>/getUpdates
   ```

   Use `result[].message.chat.id` as `TELEGRAM_CHAT_ID`.
3. Make this repository public. Public repositories have unlimited free GitHub Actions minutes; a 5-minute cron can exceed the private repository free tier.
4. Add repository secrets in GitHub under **Settings -> Secrets and variables -> Actions -> Secrets -> Repository secrets**. Use **New repository secret**, not environment secrets and not variables:

   ```text
   TELEGRAM_BOT_TOKEN
   ```

5. Optional: add `TELEGRAM_CHAT_ID` only if you want every newly opened slot alert sent to one default chat, group, or public channel. For a public channel, add the bot as an admin and use the channel username, for example `@your_channel_name`.
6. Run **Actions -> Visa Slot Monitor -> Run workflow** once to test.

## Public Bot Commands

Anyone can open your Telegram bot and send:

```text
/start
/status
/subscribe
/unsubscribe
```

`/status` returns the currently open Japan visa dates. `/subscribe` stores that chat in state so the bot sends newly opened slot alerts to that user or group. This is not bound to a single `TELEGRAM_CHAT_ID`.

Simple production mode uses GitHub Actions polling. It needs only `TELEGRAM_BOT_TOKEN`, checks every 60 seconds, answers commands, and sends subscribed users hourly status updates or immediate new-slot alerts. If a stale Telegram webhook exists, polling mode deletes it automatically and retries.

## Local Use

Install dependencies:

```sh
python -m pip install -r requirements.txt
```

Run one check:

```sh
python check_slots.py
```

Run continuously, checking every 60 seconds:

```sh
python check_slots.py --loop 60
```

If `TELEGRAM_BOT_TOKEN` is missing, the script cannot answer public commands. If `TELEGRAM_CHAT_ID` is missing, broadcast alerts to a fixed chat are disabled, but `/status` and `/subscribe` still work when the bot token is configured.

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
TELEGRAM_CHAT_ID= optional default alert target
TELEGRAM_WEBHOOK_SECRET= optional webhook secret token
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
CRON_SECRET= only for Vercel /api/check
```

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
python check_slots.py --loop 60
```

as a `systemd` service. The monitor no longer needs Playwright or Chromium, so it has a small Python/requests footprint and is suitable for a free-tier VM.

GitHub Actions remains a useful backup host. The included workflow starts on a 15-minute schedule, then keeps checking every 60 seconds for about 5 hours and 50 minutes. The repository concurrency setting allows only one active monitor and one pending replacement run, so it behaves like a near-continuous backup loop instead of a one-shot cron. GitHub scheduling can still lag or skip under load.

## Optional Vercel Pro

The repo also includes a Vercel Python entrypoint at `api/index.py` with three optional routes:

- `/api/check`: protected cron endpoint. It checks slots every minute on Vercel Pro production deployments.
- `/api/telegram`: Telegram webhook endpoint. It answers `/status`, `/subscribe`, and `/unsubscribe` immediately.
- `/api/health`: non-secret deployment health endpoint. It reports whether Telegram, Supabase, webhook, and cron secrets are configured.

For Vercel to send Telegram messages and remember subscribers, it needs a durable state backend such as Supabase or another database. If you do not have a database available, use the GitHub Actions polling mode above.

The Vercel function is protected by `CRON_SECRET`: cron invocations include `Authorization: Bearer $CRON_SECRET`, and other requests are rejected when the secret is configured.

Runbook:

```bash
# one-time
npm i -g vercel
vercel login
vercel link                      # link repo to the Pro project

# create the Supabase table by running supabase/visa_slot_state.sql in the SQL editor:
#   create table if not exists visa_slot_state (
#     key text primary key,
#     value jsonb not null default '{}'::jsonb,
#     updated_at timestamptz not null default now()
#   );
#   alter table visa_slot_state enable row level security;   -- no policies = service-key only

# prompts locally for Telegram/Supabase secrets, writes them to Vercel and GitHub,
# redeploys production, and registers the Telegram webhook
python scripts/bootstrap_vercel_production.py --url https://japanvisa-nine.vercel.app
```

Notes:

- Cron jobs run on production only.
- Changing the schedule requires a redeploy because cadence lives in `vercel.json`.
- When `TELEGRAM_WEBHOOK_SECRET` is set, cron skips `getUpdates`; command handling is done by `/api/telegram`.
- The Supabase service role key bypasses RLS. Enable RLS on `visa_slot_state` and add no anon/public policies so only the server-side service key can access the table.
- The existing GitHub Actions workflow can use the same Supabase environment variables and `STATE_KEY=event-20` as a backup watcher. It will dedupe against Vercel runs and avoid double alerts.
- After webhook setup, GitHub Actions must also receive `TELEGRAM_WEBHOOK_SECRET`; otherwise `getUpdates` conflicts with the webhook. The bootstrap script handles this unless `--skip-github-secrets` is passed.

## State

Without Supabase env vars, the monitor writes currently open dates, Telegram update offset, subscribed chat ids, and last status timestamp to `state.json`. With `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, it writes the current `{date: alt}` map to `visa_slot_state` under `STATE_KEY` and Telegram command metadata under a derived key. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again. If no new slot appears, a status message is sent after `STATUS_INTERVAL_SECONDS`.

## Limitations

GitHub scheduled workflows can lag or skip under load. For second-level speed, run `python check_slots.py --loop 60` on a VPS. Scheduled workflows can pause after 60 days of repository inactivity.
