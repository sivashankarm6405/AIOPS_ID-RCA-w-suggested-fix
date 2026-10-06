"""Day 1 checks: local LLM (qwen2.5:3b) and Telegram. Delete after Day 1 if you like."""
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")


def check_ollama() -> bool:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
    except Exception:
        print(f"FAIL  Ollama not reachable at {OLLAMA_URL} (is it running?)")
        return False
    names = [m["name"] for m in tags.get("models", [])]
    if not any(n == MODEL or n.startswith(MODEL + ":") or n.split(":")[0] == MODEL for n in names):
        print(f"FAIL  model '{MODEL}' not found. Installed: {names}. Run: ollama pull {MODEL}")
        return False
    print(f"OK    Ollama running, model '{MODEL}' is installed")

    prompt = (
        'Return JSON only, in this shape: {"cause": "<short phrase>", "confidence": <number 0-1>}. '
        "Scenario: container memory rose from 40% to 98% over 10 minutes and OOMKilled is true."
    )
    start = time.time()
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_ctx": 4096},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=240,
        )
        r.raise_for_status()
        content = r.json()["message"]["content"]
    except Exception as e:
        print(f"FAIL  chat request failed: {type(e).__name__}")
        return False
    elapsed = time.time() - start
    try:
        data = json.loads(content)
        assert "cause" in data and "confidence" in data
    except Exception:
        print(f"FAIL  model replied but not in the expected JSON shape:\n      {content[:200]}")
        return False
    print(f"OK    JSON reply in {elapsed:.1f}s -> {data}")
    if elapsed > 60:
        print("WARN  slow (>60s). First call loads the model; run again to see the warm speed.")
    return True


def check_telegram() -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or token.startswith("put-your") or not chat_id:
        print("FAIL  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in brain/.env")
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        me = requests.get(f"{base}/getMe", timeout=10).json()
        if not me.get("ok"):
            print("FAIL  Telegram rejected the token (check it in .env)")
            return False
        sent = requests.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": "AutoHeal Lite setup check: Telegram works ✅"},
            timeout=10,
        ).json()
    except Exception as e:
        print(f"FAIL  could not reach Telegram: {type(e).__name__}")
        return False
    if not sent.get("ok"):
        print(f"FAIL  token is valid but message failed: {sent.get('description')}")
        print("      Open your bot in Telegram and press Start, then check the chat ID.")
        return False
    print(f"OK    Telegram bot @{me['result']['username']} sent a test message")
    return True


if __name__ == "__main__":
    results = [check_ollama(), check_telegram()]
    print("\nAll checks passed." if all(results) else "\nFix the FAIL lines above, then run again.")