"""Minimal decision engine: dedupe alerts and build one standard incident object.
If you already have your own engine, keep process_alert()'s return shape and merge your logic in."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
INCIDENT_DIR = DATA_DIR / "incidents"
INCIDENT_DIR.mkdir(parents=True, exist_ok=True)

_open = {}  # (alert_name, service) -> incident dict


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(incident_id):
    return INCIDENT_DIR / f"{incident_id}_incident.json"


def _save(inc):
    _path(inc["incident_id"]).write_text(json.dumps(inc, indent=2))


def _next_id():
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    count = len(list(INCIDENT_DIR.glob(f"INC-{day}-*_incident.json"))) + 1
    return f"INC-{day}-{count:03d}"


def _load_open_from_disk():
    for p in INCIDENT_DIR.glob("*_incident.json"):
        inc = json.loads(p.read_text())
        if inc.get("status") == "open":
            _open[(inc["alert_name"], inc["service"])] = inc


_load_open_from_disk()


def process_alert(alert: dict):
    """Returns (event, incident). event: opened | duplicate | resolved | ignored"""
    labels = alert.get("labels", {})
    name = labels.get("alertname", "unknown")
    service = labels.get("service", "unknown")
    key = (name, service)

    if alert.get("status") == "resolved":
        inc = _open.pop(key, None)
        if inc is None:
            return "ignored", None
        inc["status"] = "resolved"
        inc["resolved_at"] = _now()
        _save(inc)
        return "resolved", inc

    if key in _open:
        return "duplicate", _open[key]

    inc = {
        "incident_id": _next_id(),
        "alert_name": name,
        "service": service,
        "severity": labels.get("severity", "warning"),
        "fault_type": labels.get("fault_type", "unknown"),
        "summary": alert.get("annotations", {}).get("summary", ""),
        "started_at": alert.get("startsAt") or _now(),
        "status": "open",
        "labels": labels,
    }
    _open[key] = inc
    _save(inc)
    return "opened", inc