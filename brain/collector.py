"""Day 2: gather evidence for an incident (metrics, logs, container state, events, changes).

Usage inside the app:  collector.collect_and_save(incident)
Manual test:           python collector.py latest        (or: python collector.py INC-20261006-001)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
WINDOW_MINUTES = 15          # how far back to look for metrics
CHANGES_WINDOW_MINUTES = 30  # how far back to look for config/deploy changes
LOG_TAIL = 200               # last N log lines
STEP_SECONDS = 15            # metric resolution

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
INCIDENT_DIR = DATA_DIR / "incidents"
CHANGES_LOG = DATA_DIR / "changes.log"

# Adding a metric later = adding one line here.
QUERIES = {
    "cpu_percent": 'rate(process_cpu_seconds_total{{service="{svc}"}}[30s]) * 100',
    "memory_bytes": 'process_resident_memory_bytes{{service="{svc}"}}',
    "memory_limit_bytes": 'app_memory_limit_bytes{{service="{svc}"}}',
    "restarts_5m": 'changes(process_start_time_seconds{{service="{svc}"}}[5m])',
    "target_up": 'up{{service="{svc}"}}',
}


def _parse_ts(value):
    """Parse '2026-10-06T10:00:00Z' (also with nanoseconds or no timezone) into UTC datetime."""
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", str(value)).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _safe(fn, *args):
    """Run one source. If it fails, record why and carry on."""
    try:
        return {"available": True, "data": fn(*args)}
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


# ---------- source 1: metrics (Prometheus) ----------
def _get_metrics(svc, start, end):
    out = {}
    for name, template in QUERIES.items():
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": template.format(svc=svc), "start": start.timestamp(),
                    "end": end.timestamp(), "step": STEP_SECONDS},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json()["data"]["result"]
        points = []
        if result:
            for ts, val in result[0]["values"]:
                v = float(val)
                if v == v and abs(v) != float("inf"):  # skip NaN / Inf
                    points.append([int(ts), round(v, 4)])
        out[name] = points  # list of [unix_time, value]
    return out


# ---------- source 2: logs (Docker) ----------
def _get_logs(client, container):
    raw = client.containers.get(container).logs(tail=LOG_TAIL, timestamps=True)
    return raw.decode("utf-8", errors="replace").splitlines()


# ---------- source 3: container state (Docker) ----------
def _get_state(client, container):
    a = client.containers.get(container).attrs
    s = a.get("State", {})
    cfg = a.get("Config", {})
    return {
        "status": s.get("Status"),
        "running": s.get("Running"),
        "oom_killed": s.get("OOMKilled"),
        "exit_code": s.get("ExitCode"),
        "restart_count": a.get("RestartCount"),
        "started_at": s.get("StartedAt"),
        "finished_at": s.get("FinishedAt"),
        "image": cfg.get("Image"),
        "memory_limit_mb": round((a.get("HostConfig", {}).get("Memory") or 0) / 1024 / 1024),
        "env_var_names": sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or [])),  # names only, never values
    }


# ---------- source 4: Docker events (oom / die / kill / start) ----------
def _get_events(client, container, start, end):
    stream = client.events(
        since=int(start.timestamp()), until=int(end.timestamp()), decode=True,
        filters={"container": container, "event": ["oom", "die", "kill", "start", "restart"]},
    )
    events = []
    for e in stream:
        attrs = e.get("Actor", {}).get("Attributes", {})
        item = {
            "time": datetime.fromtimestamp(e["time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": e.get("Action") or e.get("status"),
        }
        if "exitCode" in attrs:
            item["exit_code"] = attrs["exitCode"]
        if "signal" in attrs:
            item["signal"] = attrs["signal"]
        events.append(item)
    return events[-30:]


# ---------- source 5: recent changes (changes.log) ----------
def _get_changes(service, ref):
    if not CHANGES_LOG.exists():
        return []
    cutoff = ref - timedelta(minutes=CHANGES_WINDOW_MINUTES)
    out = []
    for line in CHANGES_LOG.read_text().splitlines():
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3:
            continue
        ts = _parse_ts(parts[0])
        if ts and cutoff <= ts <= ref and parts[1] in (service, "all"):
            out.append({"time": parts[0], "service": parts[1], "change": parts[2]})
    return out


def collect(incident: dict) -> dict:
    service = incident.get("service", "api")
    container = incident.get("labels", {}).get("container", service)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=WINDOW_MINUTES)

    client, docker_error = None, None
    try:
        import docker  # imported here so the app still starts if the package is missing
        client = docker.from_env()
        client.ping()
    except Exception as e:
        docker_error = f"{type(e).__name__}: {str(e)[:150]}"

    def from_docker(fn, *extra):
        if client is None:
            return {"available": False, "error": docker_error}
        return _safe(fn, client, container, *extra)

    return {
        "incident_id": incident.get("incident_id"),
        "collected_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "minutes": WINDOW_MINUTES},
        "metrics": _safe(_get_metrics, service, start, end),
        "logs": from_docker(_get_logs),
        "container_state": from_docker(_get_state),
        "docker_events": from_docker(_get_events, start, end),
        "recent_changes": _safe(_get_changes, service, end),
    }


def collect_and_save(incident: dict):
    """Called in the background when an incident opens. Never raises."""
    try:
        evidence = collect(incident)
        INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
        path = INCIDENT_DIR / f"{incident['incident_id']}_raw.json"
        path.write_text(json.dumps(evidence, indent=2))
        status = {k: ("ok" if evidence[k]["available"] else "UNAVAILABLE")
                  for k in ("metrics", "logs", "container_state", "docker_events", "recent_changes")}
        print(f"[collector] {incident['incident_id']} saved -> {path.name} | {status}")
        for k, v in evidence.items():
            if isinstance(v, dict) and v.get("available") is False:
                print(f"[collector]   {k}: {v['error']}")
        return path
    except Exception as e:
        print(f"[collector] failed for {incident.get('incident_id')}: {type(e).__name__}")
        return None


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "latest"
    if arg == "latest":
        files = sorted(INCIDENT_DIR.glob("*_incident.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            sys.exit("No incident files found in data/incidents")
        target = files[-1]
    else:
        target = INCIDENT_DIR / f"{arg}_incident.json"
    collect_and_save(json.loads(target.read_text()))