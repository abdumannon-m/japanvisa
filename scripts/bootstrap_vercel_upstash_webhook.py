#!/usr/bin/env python3
"""Configure Vercel + GitHub for the Upstash-backed Telegram webhook."""

from __future__ import annotations

import argparse
import getpass
import secrets
import subprocess
from urllib.parse import urlparse

from set_telegram_webhook import set_webhook


DEFAULT_SCOPE = "abdumannonmurodiy-3405s-projects"
DEFAULT_URL = "https://japanvisa-nine.vercel.app"
DEFAULT_REPO = "abdumannon-m/japanvisa"


def require_https_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit(f"{label} must be an https URL")
    return value.rstrip("/")


def prompt_secret(label: str) -> str:
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise SystemExit(f"{label} is required")
    return value


def set_vercel_env(name: str, value: str, scope: str, sensitive: bool = True) -> None:
    cmd = [
        "vercel",
        "env",
        "add",
        name,
        "production",
        "--yes",
        "--force",
        "--scope",
        scope,
    ]
    if not sensitive:
        cmd.append("--no-sensitive")

    print(f"Setting Vercel env {name}")
    subprocess.run(cmd, input=value + "\n", text=True, check=True)


def set_github_secret(name: str, value: str, repo: str) -> None:
    print(f"Setting GitHub secret {name}")
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        input=value + "\n",
        text=True,
        check=True,
    )


def deploy(scope: str) -> str | None:
    print("Deploying Vercel production")
    result = subprocess.run(
        ["vercel", "deploy", "--prod", "--yes", "--scope", scope],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    print(result.stdout)

    for line in result.stdout.splitlines():
        if line.startswith("Aliased: https://"):
            return line.removeprefix("Aliased: ").strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the Upstash-backed Telegram webhook deployment.")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="Vercel scope/team slug")
    parser.add_argument("--url", default=DEFAULT_URL, help="Production Vercel URL to register with Telegram")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository for backup workflow secrets")
    parser.add_argument(
        "--skip-github-secrets",
        action="store_true",
        help="Do not write secrets to GitHub Actions",
    )
    args = parser.parse_args()

    production_url = require_https_url(args.url, "Production URL")
    token = prompt_secret("Telegram bot token")
    upstash_url = require_https_url(input("Upstash Redis REST URL: ").strip(), "Upstash Redis REST URL")
    upstash_token = prompt_secret("Upstash Redis REST token")
    chat_id = input("Optional default Telegram chat id/channel (blank to skip): ").strip()
    webhook_secret = secrets.token_urlsafe(32)
    cron_secret = secrets.token_urlsafe(32)

    values = {
        "EVENT_ID": ("20", False),
        "CATEGORY_ID": ("12", False),
        "PLAN_ID": ("19", False),
        "MONTHS_AHEAD": ("2", False),
        "EVENT_LABEL": ("Short stay - Applicant", False),
        "MONTH_PARAM": ("date", False),
        "STATUS_INTERVAL_SECONDS": ("3600", False),
        "STATE_KEY": ("event-20", False),
        "STATE_FILE": ("/tmp/state.json", False),
        "TELEGRAM_BOT_TOKEN": (token, True),
        "TELEGRAM_WEBHOOK_SECRET": (webhook_secret, True),
        "UPSTASH_REDIS_REST_URL": (upstash_url, True),
        "UPSTASH_REDIS_REST_TOKEN": (upstash_token, True),
        "CRON_SECRET": (cron_secret, True),
    }
    if chat_id:
        values["TELEGRAM_CHAT_ID"] = (chat_id, True)

    for name, (value, sensitive) in values.items():
        set_vercel_env(name, value, args.scope, sensitive=sensitive)

    if not args.skip_github_secrets:
        github_secret_names = [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "UPSTASH_REDIS_REST_URL",
            "UPSTASH_REDIS_REST_TOKEN",
        ]
        if chat_id:
            github_secret_names.append("TELEGRAM_CHAT_ID")
        for name in github_secret_names:
            set_github_secret(name, values[name][0], args.repo)

    deployed_url = deploy(args.scope) or production_url
    result = set_webhook(deployed_url, token, webhook_secret)
    print(f"Registered Telegram webhook: {result['url']}")
    print("Done. Restart the GitHub Actions monitor so it uses TELEGRAM_WEBHOOK_SECRET and Upstash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
