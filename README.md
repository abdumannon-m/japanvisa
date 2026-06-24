# Japan Visa Slot Monitor

Notify-only Telegram monitor for the Embassy of Japan in Uzbekistan reservation calendar. It watches the short-stay visa Applicant calendar and sends a message when a newly opened appointment day appears.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Send a message to the bot, then get your chat id from:

   ```text
   https://api.telegram.org/bot<token>/getUpdates
   ```

   Use `result[].message.chat.id`.
3. Make this repository public. Public repositories have unlimited free GitHub Actions minutes; a 5-minute cron can exceed the private repository free tier.
4. Add repository secrets in GitHub under **Settings -> Secrets and variables -> Actions**:

   ```text
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
   ```

5. Run **Actions -> Visa Slot Monitor -> Run workflow** once to test.

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

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is missing, the script prints the Telegram message as a dry run instead of failing.

## Configuration

Environment variables:

```text
EVENT_ID=20
MONTHS_AHEAD=2
EVENT_LABEL=Short stay - Applicant
STATE_FILE=state.json
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

The default `EVENT_ID=20` is short-stay visa Applicant.

## State

The monitor writes currently open dates to `state.json`. New alerts are sent only for dates that were not present in the previous state, so an open day does not repeat on later runs while it stays open. If a day closes and later reopens, it is alerted again.

## Limitations

GitHub cron has roughly 5-minute granularity and can lag or skip under load. For second-level speed, run `python check_slots.py --loop 60` on a VPS or Raspberry Pi. Scheduled workflows can pause after 60 days of repository inactivity.
