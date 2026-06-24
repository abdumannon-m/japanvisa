# Japan Visa Slot Monitor

Telegram monitor for the Embassy of Japan in Uzbekistan reservation calendar. It watches the short-stay visa Applicant calendar, answers public `/status` requests, and sends subscribed users a message when a newly opened appointment day appears.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Send a message to the bot, then get your chat id from:

   ```text
   https://api.telegram.org/bot<token>/getUpdates
   ```

   Use `result[].message.chat.id`.
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

Install dependencies and the browser:

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
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
MONTHS_AHEAD=2
EVENT_LABEL=Short stay - Applicant
STATE_FILE=state.json
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID= optional default alert target
```

The default `EVENT_ID=20` is short-stay visa Applicant.

## State

The monitor writes currently open dates, Telegram update offset, and subscribed chat ids to `state.json`. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again.

## Limitations

GitHub cron has roughly 5-minute granularity and can lag or skip under load. For second-level speed, run `python check_slots.py --loop 60` on a VPS or Raspberry Pi. Scheduled workflows can pause after 60 days of repository inactivity.
