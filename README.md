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

On GitHub Actions, commands are answered when the workflow runs, so replies can take up to around 5 minutes. For faster replies, run the bot continuously on a VPS with `python check_slots.py --loop 60`.

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
STATE_FILE=state.json
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID= optional default alert target
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

## State

The monitor writes currently open dates, Telegram update offset, and subscribed chat ids to `state.json`. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again.

## Limitations

GitHub cron has roughly 5-minute granularity and can lag or skip under load. For second-level speed, run `python check_slots.py --loop 60` on a VPS. Scheduled workflows can pause after 60 days of repository inactivity.
