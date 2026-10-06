"""Sends messages. Day 1: Telegram only. Add email/Discord later in CHANNELS."""
import os

import requests
from dotenv import load_dotenv

load_dotenv()


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or token.startswith("put-your"):
        print("[telegram not configured, printing instead]\n" + text + "\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        # Do not print the exception itself: its URL contains the bot token.
        print(f"[telegram send failed: {type(e).__name__}]")
        return False


CHANNELS = {"telegram": send_telegram}


def notify(text: str) -> None:
    for name in os.getenv("NOTIFY_CHANNELS", "telegram").split(","):
        name = name.strip()
        sender = CHANNELS.get(name)
        if sender is None:
            print(f"[unknown channel: {name}]")
            continue
        try:
            sender(text)
        except Exception as e:  # one broken channel must not block the others
            print(f"[{name} failed: {type(e).__name__}]")