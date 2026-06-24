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

`/status` returns the currently open Japan visa dates. `/subscribe` stores that chat in `state.json` so the bot sends newly opened slot alerts to that user or group. This is not bound to a single `TELEGRAM_CHAT_ID`.

On GitHub Actions, commands are answered only when a workflow run starts. GitHub scheduled runs can be delayed or skipped, so this is not a true 24/7 bot runtime. For reliable minute-level checks, use Vercel Pro cron or run the bot continuously on a VPS with `python check_slots.py --loop 60`.

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
MONTHS_AHEAD=2
EVENT_LABEL=Short stay - Applicant
MONTH_PARAM=date
STATUS_INTERVAL_SECONDS=3600
STATE_FILE=state.json
STATE_KEY=event-20
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID= optional default alert target
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
CRON_SECRET= only for Vercel /api/check
```

The default `EVENT_ID=20` is the UI tab used for the booking link and Referer header. The default `CATEGORY_ID=12` is the actual short-stay visa Applicant calendar category used by the AJAX endpoint. `MONTH_PARAM=date` is the discovered next-month request field and can be overridden if the embassy changes the endpoint.

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

GitHub Actions remains a useful backup host. It runs from the included workflow every 5 minutes, but GitHub cron can lag or skip under load, and command replies are only processed when a workflow run starts.

## Vercel Pro

The repo also includes a Vercel Python function at `/api/check` and `vercel.json` cron configuration for Vercel Pro. The cron runs every minute on production deployments and calls the same `cycle()` function as the CLI. New slot alerts are sent immediately on the next cron tick; if nothing changes, a status message is sent after `STATUS_INTERVAL_SECONDS` seconds.

The Vercel function is protected by `CRON_SECRET`: cron invocations include `Authorization: Bearer $CRON_SECRET`, and other requests are rejected when the secret is configured.

Runbook:

```bash
# one-time
npm i -g vercel
vercel login
vercel link                      # link repo to the Pro project

# create the Supabase table (SQL editor):
#   create table if not exists visa_slot_state (
#     key text primary key,
#     value jsonb not null default '{}'::jsonb,
#     updated_at timestamptz not null default now()
#   );
#   alter table visa_slot_state enable row level security;   -- no policies = service-key only

# secrets (run once each, paste value when prompted)
vercel env add TELEGRAM_BOT_TOKEN production
vercel env add TELEGRAM_CHAT_ID production
vercel env add SUPABASE_URL production
vercel env add SUPABASE_SERVICE_KEY production
vercel env add CRON_SECRET production           # any long random string
vercel env add CATEGORY_ID production            # 12
vercel env add MONTHS_AHEAD production           # 2
vercel env add EVENT_LABEL production            # Short stay - Applicant
vercel env add MONTH_PARAM production            # date
vercel env add STATUS_INTERVAL_SECONDS production # 3600

vercel deploy --prod             # cron only runs on production deployments
```

Notes:

- Cron jobs run on production only.
- Changing the schedule requires a redeploy because cadence lives in `vercel.json`.
- The Supabase service role key bypasses RLS. Enable RLS on `visa_slot_state` and add no anon/public policies so only the server-side service key can access the table.
- The existing GitHub Actions workflow can use the same Supabase environment variables and `STATE_KEY=event-20` as a backup watcher. It will dedupe against Vercel runs and avoid double alerts.

## State

Without Supabase env vars, the monitor writes currently open dates, Telegram update offset, subscribed chat ids, and last status timestamp to `state.json`. With `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, it writes the current `{date: alt}` map to `visa_slot_state` under `STATE_KEY` and Telegram command metadata under a derived key. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again. If no new slot appears, a status message is sent after `STATUS_INTERVAL_SECONDS`.

## Limitations

GitHub cron has roughly 5-minute granularity and can lag or skip under load. For second-level speed, run `python check_slots.py --loop 60` on a VPS. Scheduled workflows can pause after 60 days of repository inactivity.
