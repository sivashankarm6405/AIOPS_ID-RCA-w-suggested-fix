"""Receives Alertmanager webhooks. Run:  uvicorn main:app --host 0.0.0.0 --port 8001"""
from fastapi import BackgroundTasks, Body, FastAPI

import collector
import context_builder
import decision_engine
import notifier

app = FastAPI(title="AutoHeal Lite")


def opened_text(inc):
    return (
        f"🚨 {inc['incident_id']} | {inc['alert_name']} | {inc['service']} | {inc['severity'].upper()}\n"
        f"{inc['summary']}\n"
        f"Started: {inc['started_at']}\n"
        f"Collecting evidence for diagnosis..."
    )


def resolved_text(inc):
    return f"✅ {inc['incident_id']} | {inc['alert_name']} resolved at {inc['resolved_at']}"


def gather_and_brief(inc):
    """Background job: Day 2 collects the evidence, Day 3 turns it into a brief."""
    collector.collect_and_save(inc)
    context_builder.build_and_save(inc)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/alerts")
def alerts(background_tasks: BackgroundTasks, payload: dict = Body(...)):
    results = []
    for alert in payload.get("alerts", []):
        event, inc = decision_engine.process_alert(alert)
        if event == "opened":
            notifier.notify(opened_text(inc))
            background_tasks.add_task(gather_and_brief, inc)  # runs after the reply is sent
        elif event == "resolved":
            notifier.notify(resolved_text(inc))
        results.append({"event": event, "incident_id": inc["incident_id"] if inc else None})
    return {"results": results}