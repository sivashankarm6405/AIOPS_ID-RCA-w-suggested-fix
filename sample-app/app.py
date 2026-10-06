"""Sample API with on-demand faults (CPU spike, memory leak, crash loop)."""
import logging
import os
import sys
import threading
import time

from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("api")
app = Flask(__name__)

MEM_LIMIT_MB = int(os.getenv("MEM_LIMIT_MB", "256"))
CRASH_STATE_FILE = os.getenv("CRASH_STATE_FILE", "/tmp/crashloop_remaining")

# Prometheus' default collectors already expose process_cpu_seconds_total,
# process_resident_memory_bytes and process_start_time_seconds.
Gauge("app_memory_limit_bytes", "Memory limit of this container").set(MEM_LIMIT_MB * 1024 * 1024)

events = {"cpu": threading.Event(), "memory": threading.Event()}
active = {"cpu": False, "memory": False}
leak = []


# ---------- fault: CPU spike ----------
def cpu_burn(seconds):
    log.warning("batch job 'report-export' started, CPU-bound loop running")
    end = time.time() + seconds
    while time.time() < end and not events["cpu"].is_set():
        sum(i * i for i in range(10000))
    active["cpu"] = False
    log.info("batch job 'report-export' finished")


# ---------- fault: memory leak ----------
def memory_leak(mb_per_sec):
    ticks = 0
    while not events["memory"].is_set():
        leak.append(b"x" * (mb_per_sec * 1024 * 1024))
        ticks += 1
        if ticks % 5 == 0:
            held = len(leak) * mb_per_sec
            writer = log.error if held > MEM_LIMIT_MB * 0.5 else log.warning
            writer("in-memory cache growing without eviction, cache_size_mb=%d", held)
        time.sleep(1)
    leak.clear()
    active["memory"] = False
    log.info("cache cleared")


# ---------- fault: crash loop ----------
def _read_remaining():
    try:
        with open(CRASH_STATE_FILE) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _write_remaining(n):
    with open(CRASH_STATE_FILE, "w") as f:
        f.write(str(n))


def _consume_crash_token():
    remaining = _read_remaining()
    if remaining <= 0:
        return False
    _write_remaining(remaining - 1)
    return True


def crash_after(delay):
    def _run():
        time.sleep(delay)
        log.error("fatal: cannot connect to database host 'db-primary', exiting")
        sys.stdout.flush()
        os._exit(1)

    threading.Thread(target=_run, daemon=True).start()


# ---------- normal endpoints ----------
@app.route("/")
def index():
    return jsonify(service="api", endpoints=["/health", "/users", "/metrics", "/fault/status"])


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/users")
def users():
    return jsonify(users=[{"id": 1, "name": "asha"}, {"id": 2, "name": "ravi"}])


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ---------- fault endpoints ----------
@app.route("/fault/cpu")
def fault_cpu():
    seconds = request.args.get("seconds", default=300, type=int)
    if active["cpu"]:
        return jsonify(status="already running"), 409
    events["cpu"].clear()
    active["cpu"] = True
    threading.Thread(target=cpu_burn, args=(seconds,), daemon=True).start()
    return jsonify(status="cpu fault started", seconds=seconds)


@app.route("/fault/memory")
def fault_memory():
    mb = request.args.get("mb_per_sec", default=2, type=int)
    if active["memory"]:
        return jsonify(status="already running"), 409
    events["memory"].clear()
    active["memory"] = True
    threading.Thread(target=memory_leak, args=(mb,), daemon=True).start()
    return jsonify(status="memory fault started", mb_per_sec=mb)


@app.route("/fault/crash")
def fault_crash():
    times = request.args.get("times", default=3, type=int)
    _write_remaining(times)
    _consume_crash_token()
    crash_after(3)
    return jsonify(status="crash loop started", crashes=times)


@app.route("/fault/stop")
def fault_stop():
    for e in events.values():
        e.set()
    _write_remaining(0)
    return jsonify(status="faults stopped (a crash already scheduled may still fire)")


@app.route("/fault/status")
def fault_status():
    return jsonify(active=active, leaked_mb=len(leak), crashes_remaining=_read_remaining())


if __name__ == "__main__":
    log.info("api starting, pid=%d", os.getpid())
    if _consume_crash_token():
        log.error("startup problem detected, will exit in 8s (crash loop in progress)")
        crash_after(8)
    app.run(host="0.0.0.0", port=5000, threaded=True)