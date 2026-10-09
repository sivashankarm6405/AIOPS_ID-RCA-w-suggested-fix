"""Stage 4: ask the local LLM (Ollama) for a diagnosis and choose ONE fix from runbooks.yml.

Usage inside the app:  rca.rca_and_save(incident)
Manual test:           python rca.py latest        (or: python rca.py INC-20261008-001)
See the prompt only:   python rca.py latest --show-prompt
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

load_dotenv()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "6144"))   # how much text the model can read at once
TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "240"))    # seconds to wait for the model
TEMPERATURE = 0.1        # low = steady, repeatable answers
MAX_ATTEMPTS = 2         # first try + one retry
MIN_CONFIDENCE = 0.4     # below this, hand over to a human instead of suggesting a fix
FALLBACK_ACTION = "investigate_manually"

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", HERE.parent / "data"))
INCIDENT_DIR = DATA_DIR / "incidents"
RUNBOOK_FILE = HERE / "runbooks.yml"


# ---------- runbooks ----------
def load_runbooks():
    with open(RUNBOOK_FILE, encoding="utf-8") as f:
        runbooks = yaml.safe_load(f)
    if FALLBACK_ACTION not in runbooks:
        raise ValueError(f"runbooks.yml must contain '{FALLBACK_ACTION}'")
    return runbooks


# ---------- what the model must return ----------
class Cause(BaseModel):
    cause: str = Field(min_length=3)
    confidence: float
    evidence: list[str] = Field(min_length=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise(cls, v):
        v = float(v)
        if 1 < v <= 100:      # small models sometimes answer 85 instead of 0.85
            v = v / 100
        if not 0 <= v <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return v


class RCA(BaseModel):
    summary: str = Field(min_length=5)
    root_causes: list[Cause] = Field(min_length=1, max_length=5)
    recommended_action: str
    reasoning: str = ""


def _schema(action_ids):
    """Tells Ollama the exact shape of the answer; the fix ID can only be one of ours."""
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "root_causes": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
                "type": "object",
                "properties": {"cause": {"type": "string"}, "confidence": {"type": "number"},
                               "evidence": {"type": "array", "items": {"type": "string"}}},
                "required": ["cause", "confidence", "evidence"]}},
            "recommended_action": {"type": "string", "enum": action_ids},
            "reasoning": {"type": "string"},
        },
        "required": ["summary", "root_causes", "recommended_action", "reasoning"],
    }


# ---------- prompt ----------
def _system_prompt(runbooks):
    menu = "\n".join(f"- {rid}: {rb['when_to_use']}" for rid, rb in runbooks.items())
    return f"""You are an SRE assistant diagnosing ONE incident in a small web service.

Rules:
- Use ONLY the evidence in the brief. Never invent numbers, log lines or events.
- If evidence is weak or a source is missing (see data_gaps), say so and lower your confidence.
- Choose exactly ONE fix from the allowed list below, using its ID exactly as written.
- All times are UTC.

Allowed fixes:
{menu}

How to read the brief:
- Memory close to its limit and still rising, with log lines showing steady growth, points to a memory leak.
- A "container oom" event or exit code 137 means the container was killed for using too much memory.
- High CPU while memory stays flat points to a runaway job or traffic spike.
- Several restarts with the same error in the logs after each start points to a crash loop.
- - A change made shortly before the problem began is a clue, but it is the cause only if the symptoms match it. Steady growth in memory is a leak, and a restart clears it for now.

Answer with JSON only, in this shape:
{{"summary": "one sentence",
  "root_causes": [{{"cause": "short description", "confidence": 0.0 to 1.0, "evidence": ["1 to 3 short facts taken from the brief"]}}],
  "recommended_action": "one ID from the allowed list",
  "reasoning": "one or two sentences: why this fix helps and what it will not solve"}}
List 1 to 3 root causes, most likely first."""


def _brief_for_prompt(brief):
    b = json.loads(json.dumps(brief))
    b.pop("meta", None)                              # bookkeeping, not evidence
    b.get("container_state", {}).pop("env_var_names", None)
    return json.dumps(b, separators=(",", ":"))


def build_messages(brief, runbooks):
    return [
        {"role": "system", "content": _system_prompt(runbooks)},
        {"role": "user", "content": f"Incident brief:\n{_brief_for_prompt(brief)}\n\nDiagnose this incident."},
    ]


# ---------- talking to Ollama ----------
def _chat(messages, schema):
    def post(fmt):
        return requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": MODEL, "stream": False, "messages": messages, "format": fmt, "keep_alive": "10m",
                  "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX}},
            timeout=TIMEOUT)

    r = post(schema)
    if r.status_code == 400:   # older Ollama versions do not accept a schema; fall back to plain JSON mode
        r = post("json")
    r.raise_for_status()
    return r.json()["message"]["content"]


def diagnose(brief, runbooks):
    """Returns (RCA or None, attempts, error_text)."""
    messages = build_messages(brief, runbooks)
    ids = list(runbooks)
    schema = _schema(ids)
    error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        content = ""
        try:
            content = _chat(messages, schema)
            data = RCA.model_validate_json(content)
            if data.recommended_action not in runbooks:
                raise ValueError(f"'{data.recommended_action}' is not in the allowed list")
            return data, attempt, ""
        except requests.RequestException as e:      # Ollama down, model missing, timeout: retrying will not help
            return None, attempt, f"Ollama problem: {type(e).__name__}: {str(e)[:120]}"
        except (ValidationError, ValueError) as e:
            error = str(e).replace("\n", " ")[:250]
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That reply was invalid ({error}). Reply again with valid JSON only, "
                                            f"and choose recommended_action from: {', '.join(ids)}."}]
    return None, MAX_ATTEMPTS, f"Model reply stayed invalid: {error}"


# ---------- final result ----------
def _fix(runbooks, rid):
    rb = runbooks[rid]
    return {"id": rid, "title": rb["title"], "risk": rb["risk"], "steps": rb["steps"]}


def analyse(brief, runbooks):
    started = time.time()
    data, attempts, error = diagnose(brief, runbooks)
    seconds = round(time.time() - started, 1)
    base = {"incident_id": brief["incident"]["incident_id"],
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": MODEL, "attempts": attempts, "seconds": seconds}

    if data is None:  # the model failed: say so honestly and hand over to a human
        return {**base, "status": "fallback", "summary": "Automatic diagnosis failed. Review the brief by hand.",
                "root_causes": [], "recommended_action": FALLBACK_ACTION, "reasoning": "", "note": error,
                "fix": _fix(runbooks, FALLBACK_ACTION)}

    causes = sorted((c.model_dump() for c in data.root_causes), key=lambda c: -c["confidence"])
    action, note, suggested = data.recommended_action, "", data.recommended_action
    if causes[0]["confidence"] < MIN_CONFIDENCE:
        action = FALLBACK_ACTION
        note = (f"The model suggested '{suggested}' but its top confidence was only "
                f"{round(causes[0]['confidence'] * 100)}%, so this was handed to a human.")
    result = {**base, "status": "ok" if attempts == 1 else "retried", "summary": data.summary, "root_causes": causes,
              "recommended_action": action, "reasoning": data.reasoning, "note": note, "fix": _fix(runbooks, action)}
    if action != suggested:
        result["model_suggested_action"] = suggested
    return result


# ---------- save + print ----------
def _ascii(text):
    return str(text).encode("ascii", "replace").decode()   # Windows cmd cannot print every character


def _print_result(r):
    print(f"[rca] {r['incident_id']} | status {r['status']} | {r['seconds']}s | attempts {r['attempts']}")
    print(f"      Summary: {_ascii(r['summary'])}")
    if r["root_causes"]:
        top = r["root_causes"][0]
        print(f"      Likely cause ({round(top['confidence'] * 100)}%): {_ascii(top['cause'])}")
        for e in top["evidence"]:
            print(f"        - {_ascii(e)}")
    print(f"      Fix: {r['fix']['title']} (risk {r['fix']['risk']})")
    if r.get("note"):
        print(f"      Note: {_ascii(r['note'])}")


def rca_and_save(incident):
    """Called in the background after the brief is saved. Never raises."""
    try:
        iid = incident["incident_id"]
        brief_path = INCIDENT_DIR / f"{iid}_brief.json"
        if not brief_path.exists():
            print(f"[rca] {iid}: no brief yet, skipping")
            return None
        result = analyse(json.loads(brief_path.read_text()), load_runbooks())
        path = INCIDENT_DIR / f"{iid}_rca.json"
        path.write_text(json.dumps(result, indent=2))
        _print_result(result)
        return path
    except Exception as e:
        print(f"[rca] failed for {incident.get('incident_id')}: {type(e).__name__}: {_ascii(str(e)[:120])}")
        return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    arg = args[0] if args else "latest"
    if arg == "latest":
        files = sorted(INCIDENT_DIR.glob("*_brief.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            sys.exit("No brief files found in data/incidents. Run the builder first.")
        iid = files[-1].name.replace("_brief.json", "")
    else:
        iid = arg
    if "--show-prompt" in sys.argv:
        msgs = build_messages(json.loads((INCIDENT_DIR / f"{iid}_brief.json").read_text()), load_runbooks())
        for m in msgs:
            print(f"----- {m['role'].upper()} -----\n{m['content']}\n")
        chars = sum(len(m["content"]) for m in msgs)
        print(f"Prompt size: {chars} characters, roughly {chars // 4} tokens (model window: {NUM_CTX} tokens)")
        sys.exit()
    path = rca_and_save({"incident_id": iid})
    if path:
        print(f"\nSaved: {path.name}")