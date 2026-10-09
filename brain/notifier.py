# Sends messages. Telegram only for now; add more channels to CHANNELS later.
#
# Preview a diagnosis message without sending it:  python notifier.py latest --preview
# Send the diagnosis for a saved incident:         python notifier.py INC-20261009-001
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

INCIDENT_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "incidents"


def _console(text: str) -> None:
    print(text.encode("ascii", "replace").decode())   # Windows cmd cannot print emoji


# ---------- sending ----------
def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or token.startswith("put-your"):
        print("[telegram not configured, printing instead]")
        _console(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},   # Telegram's limit is 4096 characters
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


# ---------- the diagnosis message ----------
def _friendly_reason(note: str) -> str:
    note = note or ""
    if note.startswith("Ollama problem"):
        return "The AI model could not be reached. Is Ollama running, and is the model installed?"
    if note.startswith("Model reply stayed invalid"):
        return "The AI answered in an unusable format twice."
    return note[:150] or "unknown"


def format_diagnosis(inc: dict, rca: dict | None) -> str:
    head = f"{inc['incident_id']} | {inc['alert_name']} | {inc['service']} | {inc['severity'].upper()}"

    if rca is None:   # the investigation itself could not finish
        lines = [f"⚠️ {head}", "",
                 "The automatic investigation could not finish.",
                 "Check the brain terminal for the reason, then review data/incidents by hand."]
    else:
        fix = rca["fix"]
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(fix["steps"][:4], 1))
        if rca["status"] == "fallback":   # the AI failed; say so honestly
            lines = [f"⚠️ {head}", "",
                     "The automatic diagnosis failed.",
                     f"Reason: {_friendly_reason(rca.get('note'))}", "",
                     f"Suggested: {fix['title']} (risk: {fix['risk']})", steps]
        else:
            top = rca["root_causes"][0]
            evidence = "\n".join(f"- {e}" for e in top["evidence"][:3])
            lines = [f"🔎 {head}", "",
                     f"Summary: {rca['summary']}", "",
                     f"Likely cause ({round(top['confidence'] * 100)}%): {top['cause']}",
                     "Evidence:", evidence, "",
                     f"🛠 Suggested fix (risk: {fix['risk']}): {fix['title']}", steps]
            if rca.get("reasoning"):
                lines += ["", f"Why: {rca['reasoning']}"]
            if rca.get("note"):
                lines += ["", f"Note: {rca['note']}"]
            lines += ["", "No action has been taken. You decide whether to apply it."]

    if inc.get("status") == "resolved":
        lines += ["", "✅ This incident has already resolved."]
    if rca is not None:
        lines += ["", f"Diagnosed by {rca['model']} in {rca['seconds']}s"]
    return "\n".join(lines)


def notify_diagnosis(inc: dict, rca: dict | None) -> None:
    notify(format_diagnosis(inc, rca))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    arg = args[0] if args else "latest"
    if arg == "latest":
        files = sorted(INCIDENT_DIR.glob("*_rca.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            sys.exit("No _rca.json files found. Run python rca.py first.")
        iid = files[-1].name.replace("_rca.json", "")
    else:
        iid = arg
    incident = json.loads((INCIDENT_DIR / f"{iid}_incident.json").read_text())
    rca_file = INCIDENT_DIR / f"{iid}_rca.json"
    result = json.loads(rca_file.read_text()) if rca_file.exists() else None
    text = format_diagnosis(incident, result)
    if "--preview" in sys.argv:
        _console(text)
    else:
        notify(text)
        print(f"Sent diagnosis for {iid}")