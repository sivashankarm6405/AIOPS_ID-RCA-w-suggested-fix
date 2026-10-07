"""Day 3: turn the raw evidence file into a short, structured brief for the LLM.

Usage inside the app:  context_builder.build_and_save(incident)
Manual test:           python context_builder.py latest        (or: python context_builder.py INC-20261007-001)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
INCIDENT_DIR = DATA_DIR / "incidents"

MAX_BRIEF_CHARS = 8000   # hard size budget (compact JSON)
KEEP_LAST_LINES = 30     # always keep the newest N log lines
MAX_LOG_GROUPS = 25      # max lines after collapsing repeats
MSG_MAX = 110            # max characters per log message
TIMELINE_MAX = 15
MEM_ALERT_PCT = 60       # derived timeline event: memory passed this % of limit
CPU_ALERT_PCT = 80       # derived timeline event: CPU passed this %

ANSI = re.compile(r"\x1b\[[0-9;]*m")
DOCKER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+")
APP_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?\s+")
LEVEL = re.compile(r"^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b\s*")
SCRAPE_NOISE = re.compile(r'"GET /(metrics|health)[ ?]')
BANNER_NOISE = re.compile(r"development server|Press CTRL\+C|Running on (all addresses|http)")
IMPORTANT = re.compile(r"Traceback|Exception|exception")


# ---------- helpers ----------
def _ts(value):
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", str(value)).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt if dt.year > 2000 else None  # Docker uses year 0001 for "never"
    except Exception:
        return None


def _hms(dt):
    return dt.strftime("%H:%M:%S") if dt else "??:??:??"


def _unix(t):
    return datetime.fromtimestamp(t, timezone.utc)


# ---------- metrics ----------
def _summarise(points, scale=1.0, min_delta=0.0, nd=1):
    vals = [v * scale for _, v in points]
    if not vals:
        return None
    n = len(vals)
    third = max(1, n // 3)
    first = sum(vals[:third]) / third
    last = sum(vals[-third:]) / third
    cur = vals[-1]
    delta = max(last - first, cur - first, key=abs)  # catches both slow drift and a late spike
    if n < 3:
        trend = "not enough data"
    elif abs(delta) < min_delta or abs(delta) < 0.1 * max(abs(first), abs(last), 1e-9):
        trend = "flat"
    else:
        trend = "rising" if delta > 0 else "falling"
    return {"current": round(cur, nd), "min": round(min(vals), nd), "max": round(max(vals), nd),
            "avg": round(sum(vals) / n, nd), "trend": trend}


def _metrics_summary(m):
    out, gaps = {}, []
    for key, pts in m.items():
        if not pts:
            gaps.append(f"metric {key}: no data")

    limit_mb = round(m["memory_limit_bytes"][-1][1] / 1048576) if m.get("memory_limit_bytes") else None
    mem = _summarise(m.get("memory_bytes", []), 1 / 1048576, 5)
    if mem:
        pct = (lambda mb: round(100 * mb / limit_mb)) if limit_mb else (lambda mb: None)
        out["memory"] = {"current_mb": mem["current"], "max_mb": mem["max"], "limit_mb": limit_mb,
                         "percent_of_limit_now": pct(mem["current"]),
                         "percent_of_limit_max": pct(mem["max"]), "trend": mem["trend"]}
    cpu = _summarise(m.get("cpu_percent", []), 1.0, 5)
    if cpu:
        out["cpu"] = {"current_pct": cpu["current"], "max_pct": cpu["max"], "avg_pct": cpu["avg"], "trend": cpu["trend"]}
    if m.get("restarts_5m"):
        out["restarts"] = {"max_in_any_5min_window": int(max(v for _, v in m["restarts_5m"]))}
    if m.get("target_up"):
        ups = [v for _, v in m["target_up"]]
        out["target_up"] = {"samples_down": sum(1 for v in ups if v < 1), "samples_total": len(ups)}
    return out, gaps, limit_mb


def _first_crossing(points, threshold):
    for ts, v in points:
        if v >= threshold:
            return _unix(ts)
    return None


# ---------- logs ----------
def _parse_log_line(line):
    line = ANSI.sub("", line)
    dt = None
    m = DOCKER_TS.match(line)
    if m:
        dt = _ts(m.group(1))
        line = line[m.end():]
    line = APP_TS.sub("", line, count=1)
    level = ""
    m = LEVEL.match(line)
    if m:
        level = m.group(1).replace("WARNING", "WARN")
        line = line[m.end():]
    return dt, level, line.strip()


def _clean_logs(lines):
    records, hidden = [], 0
    for raw in lines:
        dt, level, msg = _parse_log_line(raw)
        if not msg:
            continue
        if SCRAPE_NOISE.search(msg) or BANNER_NOISE.search(msg):
            hidden += 1
            continue
        records.append((dt, level, msg))

    keep = set(range(max(0, len(records) - KEEP_LAST_LINES), len(records)))
    keep |= {i for i, (_, lv, msg) in enumerate(records) if lv in ("WARN", "ERROR", "CRITICAL") or IMPORTANT.search(msg)}

    groups = {}  # collapse repeats: same level + same text once digits are ignored
    for i in sorted(keep):
        dt, level, msg = records[i]
        key = (level, re.sub(r"\d+", "#", msg))
        g = groups.setdefault(key, {"level": level, "first": msg, "last": msg, "t1": dt, "t2": dt, "n": 0})
        g["last"], g["t2"] = msg, dt or g["t2"]
        g["n"] += 1

    lines_out = []
    for g in list(groups.values())[-MAX_LOG_GROUPS:]:
        t = _hms(g["t1"]) if g["t1"] == g["t2"] else f"{_hms(g['t1'])}-{_hms(g['t2'])}"
        lvl = f"{g['level']} " if g["level"] else ""
        first, last = g["first"][:MSG_MAX], g["last"][:MSG_MAX]
        if g["n"] == 1:
            lines_out.append(f"[{t}] {lvl}{first}")
        elif first == last:
            lines_out.append(f"[{t}] {lvl}{first} (x{g['n']})")
        else:
            lines_out.append(f"[{t}] {lvl}(x{g['n']}) first: {first} | last: {last}")
    return lines_out, hidden


# ---------- timeline ----------
def _timeline(incident, raw, mem_points, cpu_points, limit_mb):
    ev = []
    t = _ts(incident.get("started_at"))
    if t:
        ev.append((t, f"alert {incident.get('alert_name')} started"))
    if raw.get("docker_events", {}).get("available"):
        for e in raw["docker_events"]["data"]:
            text = f"container {e['event']}"
            if "exit_code" in e:
                text += f" (exit code {e['exit_code']})"
            if "signal" in e:
                text += f" (signal {e['signal']})"
            if _ts(e["time"]):
                ev.append((_ts(e["time"]), text))
    if raw.get("container_state", {}).get("available"):
        s = raw["container_state"]["data"]
        if _ts(s.get("started_at")):
            ev.append((_ts(s["started_at"]), "container last started"))
        if _ts(s.get("finished_at")):
            ev.append((_ts(s["finished_at"]), "container last stopped"))
    if raw.get("recent_changes", {}).get("available"):
        for c in raw["recent_changes"]["data"]:
            if _ts(c["time"]):
                ev.append((_ts(c["time"]), f"change: {c['change']}"))
    if limit_mb and mem_points:
        x = _first_crossing(mem_points, limit_mb * 1048576 * MEM_ALERT_PCT / 100)
        if x:
            ev.append((x, f"memory passed {MEM_ALERT_PCT}% of limit"))
    if cpu_points:
        x = _first_crossing(cpu_points, CPU_ALERT_PCT)
        if x:
            ev.append((x, f"CPU passed {CPU_ALERT_PCT}%"))
    ev.sort(key=lambda e: e[0])
    return [f"{_hms(t)} {text}" for t, text in ev][-TIMELINE_MAX:]


# ---------- budget ----------
def _size(brief):
    return len(json.dumps(brief, separators=(",", ":")))


def _fit_budget(brief):
    trimmed = False
    logs = brief["key_log_lines"]
    while _size(brief) > MAX_BRIEF_CHARS and len(logs) > 8:       # 1. drop oldest non-error log lines
        idx = next((i for i, l in enumerate(logs) if "ERROR" not in l and "WARN" not in l), 0)
        logs.pop(idx)
        trimmed = True
    if _size(brief) > MAX_BRIEF_CHARS:                            # 2. shorten the timeline
        brief["timeline"] = brief["timeline"][-10:]
        trimmed = True
    if _size(brief) > MAX_BRIEF_CHARS:                            # 3. drop min/avg detail
        for v in brief["metrics_summary"].values():
            for k in ("min", "avg", "avg_pct"):
                v.pop(k, None)
        trimmed = True
    brief["meta"]["trimmed"] = trimmed
    brief["meta"]["chars"] = _size(brief)
    return brief


# ---------- main build ----------
def build(raw: dict, incident: dict) -> dict:
    gaps = []
    for name in ("metrics", "logs", "container_state", "docker_events", "recent_changes"):
        sec = raw.get(name, {})
        if not sec.get("available"):
            gaps.append(f"{name}: unavailable ({str(sec.get('error', 'missing'))[:60]})")

    metrics_summary, mem_pts, cpu_pts, limit_mb = {}, [], [], None
    if raw.get("metrics", {}).get("available"):
        m = raw["metrics"]["data"]
        metrics_summary, mgaps, limit_mb = _metrics_summary(m)
        gaps += mgaps
        mem_pts, cpu_pts = m.get("memory_bytes", []), m.get("cpu_percent", [])

    log_lines, hidden = [], 0
    if raw.get("logs", {}).get("available"):
        log_lines, hidden = _clean_logs(raw["logs"]["data"])

    state = {}
    if raw.get("container_state", {}).get("available"):
        s = raw["container_state"]["data"]
        state = {k: s.get(k) for k in ("status", "exit_code", "restart_count", "oom_killed",
                                       "memory_limit_mb", "started_at", "finished_at", "env_var_names")}
        if not _ts(state["finished_at"]):
            state["finished_at"] = None

    changes = raw["recent_changes"]["data"] if raw.get("recent_changes", {}).get("available") else []

    brief = {
        "incident": {k: incident.get(k) for k in ("incident_id", "alert_name", "service", "severity", "summary", "started_at")},
        "metrics_summary": metrics_summary,
        "container_state": state,
        "key_log_lines": log_lines,
        "recent_changes": changes,
        "timeline": _timeline(incident, raw, mem_pts, cpu_pts, limit_mb),
        "data_gaps": gaps,
        "meta": {"times_are": "UTC", "window_minutes": raw.get("window", {}).get("minutes"),
                 "scrape_lines_hidden": hidden, "trimmed": False, "chars": 0},
    }
    return _fit_budget(brief)


def build_and_save(incident: dict):
    """Called in the background after the collector. Never raises."""
    try:
        iid = incident["incident_id"]
        raw_path = INCIDENT_DIR / f"{iid}_raw.json"
        if not raw_path.exists():
            print(f"[builder] {iid}: no raw file yet, skipping")
            return None
        brief = build(json.loads(raw_path.read_text()), incident)
        path = INCIDENT_DIR / f"{iid}_brief.json"
        path.write_text(json.dumps(brief, indent=2))
        status = "within budget" if brief["meta"]["chars"] <= MAX_BRIEF_CHARS else "OVER BUDGET"
        print(f"[builder] {iid} saved -> {path.name} | {brief['meta']['chars']} chars "
              f"(budget {MAX_BRIEF_CHARS}, {status}) | log lines {len(brief['key_log_lines'])} | gaps {len(brief['data_gaps'])}")
        return path
    except Exception as e:
        print(f"[builder] failed for {incident.get('incident_id')}: {type(e).__name__}: {str(e)[:100]}")
        return None


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "latest"
    if arg == "latest":
        files = sorted(INCIDENT_DIR.glob("*_raw.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            sys.exit("No raw files found in data/incidents. Run the collector first.")
        iid = files[-1].name.replace("_raw.json", "")
    else:
        iid = arg
    inc_file = INCIDENT_DIR / f"{iid}_incident.json"
    incident = json.loads(inc_file.read_text()) if inc_file.exists() else {"incident_id": iid}
    out = build_and_save(incident)
    if out:
        print(out.read_text())