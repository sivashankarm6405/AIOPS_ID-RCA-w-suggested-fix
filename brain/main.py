"""Receives Alertmanager webhooks. Run:  uvicorn main:app --host 0.0.0.0 --port 8001"""
from fastapi import Body, FastAPI

import decision_engine
import notifier

app = FastAPI(title="AutoHeal Lite")


def opened_text(inc):
    return (
        f"🚨 {inc['incident_id']} | {inc['alert_name']} | {inc['service']} | {inc['severity'].upper()}\n"
        f"{inc['summary']}\n"
        f"Started: {inc['started_at']}\n"
        f"(Evidence collection comes in Day 2)"
    )


def resolved_text(inc):
    return f"✅ {inc['incident_id']} | {inc['alert_name']} resolved at {inc['resolved_at']}"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/alerts")
def alerts(payload: dict = Body(...)):
    results = []
    for alert in payload.get("alerts", []):
        event, inc = decision_engine.process_alert(alert)
        if event == "opened":
            notifier.notify(opened_text(inc))
        elif event == "resolved":
            notifier.notify(resolved_text(inc))
        results.append({"event": event, "incident_id": inc["incident_id"] if inc else None})
    return {"results": results}