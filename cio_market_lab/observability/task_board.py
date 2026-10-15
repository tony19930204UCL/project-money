"""Local read-only task observatory. No scheduler or mutation surface."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT = 1024 * 1024
HERE = Path(__file__).resolve().parent
SOURCES = ("commitment_ledger", "obligation_registry", "relay_plan", "cron_jobs", "roadmap")
FIELDS = ("id", "title", "project", "stage", "lifecycle", "owner", "priority", "due_at",
          "last_activity_at", "latest_result", "acceptance_scope", "dependencies",
          "blocker", "executor_kind", "executor_ref", "executor_health", "source_updated_at")
def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try: return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError): return None
    if isinstance(value, str):
        try: return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc).isoformat()
        except ValueError: return None
    return None

def read_json(path):
    p = Path(path)
    if not p.is_file() or p.stat().st_size > LIMIT: raise ValueError("source unavailable or oversized")
    with p.open("rb") as f: data = f.read(LIMIT + 1)
    if len(data) > LIMIT: raise ValueError("source oversized")
    return json.loads(data)

def health(kind, ref, start_token=None):
    if kind == "cron": return "SCHEDULED"
    if kind != "pid": return "UNVERIFIED"
    try: pid = int(ref)
    except (TypeError, ValueError): return "UNKNOWN"
    if pid <= 0: return "UNKNOWN"
    if os.name == "posix":
        try:
            stat = Path("/proc") / str(pid) / "stat"
            if stat.exists():
                fields = stat.read_text().split(") ", 1)[1].split()
                if fields[0] in ("Z", "X"): return "DEAD"
                if start_token is not None and str(fields[19]) != str(start_token): return "PID_REUSED"
        except PermissionError: return "UNKNOWN"
        except (OSError, IndexError, ValueError): pass
    try: os.kill(pid, 0)
    except ProcessLookupError: return "DEAD"
    except PermissionError: return "UNKNOWN"
    except OSError: return "UNKNOWN"
    return "PROCESS_EXISTS_UNVERIFIED"

def clean(value):
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    return None

def dependency_list(value):
    if value is None: return []
    if isinstance(value, (str, int, dict)): value = [value]
    if not isinstance(value, list): raise ValueError("invalid dependencies")
    result = []
    for entry in value:
        if isinstance(entry, dict): entry = entry.get("id", entry.get("obligation_id"))
        if not isinstance(entry, (str, int)): raise ValueError("invalid dependency")
        result.append(str(entry)[:160])
    return result[:30]

def normalize(item, source):
    handle = item.get("handle") if isinstance(item.get("handle"), dict) else (item.get("execution_handle") if isinstance(item.get("execution_handle"), dict) else {})
    kind = str(handle.get("kind") or item.get("executor_kind") or ("cron" if source == "cron_jobs" else "manual"))
    ref = handle.get("ref", item.get("executor_ref"))
    state = str(item.get("state") or item.get("status") or item.get("lifecycle") or "UNKNOWN").upper()
    record = {
        "id": str(item.get("id") or item.get("obligation_id") or item.get("name") or "")[:160], "title": str(item.get("text") or item.get("title") or item.get("name") or item.get("obligation_id") or item.get("id") or "未命名")[:300],
        "project": clean(item.get("project")), "stage": clean(item.get("stage")),
        "lifecycle": state, "owner": clean(item.get("owner")), "priority": clean(item.get("priority")),
        "due_at": timestamp(item.get("due", item.get("deadline", item.get("next_run_at", item.get("due_at"))))),
        "last_activity_at": timestamp(item.get("updated", item.get("updated_at", item.get("last_run_at", item.get("last_activity_at"))))),
        "latest_result": clean(item.get("latest_result") or item.get("last_status") or item.get("result") or item.get("reason")),
        "acceptance_scope": clean(item.get("acceptance_scope") or item.get("accept")),
        "dependencies": dependency_list(item.get("blocked_by", item.get("dependencies"))),
        "blocker": clean(item.get("blocker") or item.get("reason") if state == "BLOCKED" else item.get("blocker")),
        "executor_kind": kind, "executor_ref": str(ref)[:100] if ref is not None and kind != "cron" else None,
        "executor_health": health(kind, ref, handle.get("start_token") or handle.get("start_time") or handle.get("start_ticks")),
        "source_updated_at": timestamp(item.get("updated", item.get("updated_at", item.get("source_updated_at")))),
        "original_deadline": timestamp(item.get("original_deadline", item.get("deadline"))),
        "original_terminal_failure": item.get("original_terminal_failure") if isinstance(item.get("original_terminal_failure"), str) else None,
        "recovery_state": item.get("recovery_state") if isinstance(item.get("recovery_state"), str) else None,
        "source": source,
    }
    record["stale"] = bool(record["source_updated_at"] and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(record["source_updated_at"])).total_seconds() > 86400)
    record["overdue"] = bool(record["due_at"] and dt.datetime.fromisoformat(record["due_at"]) < dt.datetime.now(dt.timezone.utc) and state not in ("DONE", "VERIFIED", "CANCELLED"))
    record["running"] = False  # PID existence alone never proves actual task execution
    record["scheduled"] = (kind == "cron" and item.get("enabled", True) is not False) or state == "SCHEDULED"
    record["awaiting_acceptance"] = state in ("DONE", "VERIFIED", "SOURCE_ACCEPTED") and record["acceptance_scope"] not in ("deployed", "client_received", "original_host_verified")
    return record

def project(config):
    threshold = config.get("freshness_threshold_seconds", 86400)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0: threshold = 86400
    tasks, diagnostics, roadmaps, seen = [], [], [], set()
    loaded = {}
    for source in SOURCES:
        path = config.get(source)
        if not path:
            diagnostics.append({"source": source, "status": "NOT_CONFIGURED"})
            continue
        try:
            data = read_json(path)
            if not isinstance(data, (dict, list)): raise ValueError("invalid top-level structure")
            expected = {"commitment_ledger": "items", "obligation_registry": "obligations", "cron_jobs": "jobs", "relay_plan": "jobs", "roadmap": "projects"}[source]
            if source == "obligation_registry" and isinstance(data, dict) and isinstance(data.get("items"), list): expected = "items"
            if not isinstance(data, dict) or not isinstance(data.get(expected), list): raise ValueError("invalid schema: expected " + expected + " list")
            loaded[source] = data
            diagnostics.append({"source": source, "status": "OK", "source_mtime": timestamp(Path(path).stat().st_mtime), "source_updated_at": timestamp(data.get("updated_at")), "stale": dt.datetime.now(dt.timezone.utc).timestamp() - Path(path).stat().st_mtime > threshold, "freshness_threshold_seconds": threshold})
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            diagnostics.append({"source": source, "status": "ERROR", "message": type(e).__name__})
    def add(items, source):
        if not isinstance(items, list): raise ValueError("items must be a list")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not (item.get("id") or item.get("obligation_id") or item.get("name")):
                diagnostics.append({"source": source, "status": "ERROR", "message": "invalid row", "index": index}); continue
            try: obj = normalize(item, source)
            except (ValueError, TypeError):
                diagnostics.append({"source": source, "status": "ERROR", "message": "invalid row", "index": index}); continue
            key = (source, obj["id"])
            if key not in seen:
                seen.add(key); tasks.append(obj)
    for source, key in (("commitment_ledger", "items"), ("obligation_registry", "obligations"), ("cron_jobs", "jobs")):
        if source in loaded:
            try: add(loaded[source].get("items" if source == "obligation_registry" and "obligations" not in loaded[source] else key, []), source)
            except (AttributeError, ValueError):
                diagnostics.append({"source": source, "status": "ERROR", "message": "invalid items"})
    # Deduplicate only explicitly linked obligations, preserving any disagreement as diagnostics.
    ledger = loaded.get("commitment_ledger", {})
    if isinstance(ledger, dict):
        for item in ledger.get("items", []):
            if not isinstance(item, dict): continue
            link = item.get("registry_link")
            if isinstance(link, dict) and link.get("obligation_id"):
                oid = str(link["obligation_id"])
                matches = [t for t in tasks if t["source"] == "obligation_registry" and t["id"] == oid]
                if matches:
                    tasks.remove(matches[0])
                    commitment = next((t for t in tasks if t["source"] == "commitment_ledger" and t["id"] == str(item.get("id"))), None)
                    if commitment:
                        commitment["registry_lifecycle"] = matches[0]["lifecycle"]
                        commitment["registry_original_deadline"] = matches[0]["due_at"]
                        commitment["registry_original_terminal_failure"] = matches[0]["lifecycle"] if matches[0]["lifecycle"].startswith("FAILED") else None
                    diagnostics.append({"source": "obligation_registry", "status": "LINKED", "id": oid})
                    commitment = next((t for t in tasks if t["source"] == "commitment_ledger" and t["id"] == str(item.get("id"))), None)
                    if commitment and commitment["lifecycle"] != matches[0]["lifecycle"]:
                        diagnostics.append({"source": "obligation_registry", "status": "CONFLICT", "id": oid, "message": "linked lifecycle disagreement"})
    relay = loaded.get("relay_plan", {})
    if isinstance(relay, dict):
        for job in relay.get("jobs", []):
            try:
                if not isinstance(job, str): raise ValueError()
                root = Path(config.get("relay_allowed_root") or Path(config["relay_plan"]).parent).resolve(strict=True)
                def confined(candidate):
                    candidate = Path(candidate)
                    if candidate.is_symlink(): raise ValueError("relay symlink denied")
                    resolved = candidate.resolve(strict=True)
                    if not resolved.is_relative_to(root): raise ValueError("relay path outside allowed root")
                    return resolved
                job = confined(job)
                job_data = read_json(job)
                state_dir = job_data.get("state_dir")
                if not isinstance(state_dir, str): raise ValueError()
                for name in ("backlog.json", "executor_latest.json", "executor_terminal.json"):
                    p = Path(state_dir) / name
                    if p.is_file():
                        data = read_json(confined(p))
                        if isinstance(data, dict):
                            if name == "backlog.json":
                                entries = data.get("items", data.get("tasks", data.get("backlog")))
                                if not isinstance(entries, list): raise ValueError("invalid relay backlog")
                                add(entries, "relay_" + name)
                            else: add([data], "relay_" + name)
                        else: raise ValueError("invalid relay payload")
            except (OSError, ValueError, TypeError, AttributeError):
                diagnostics.append({"source": "relay_plan", "status": "ERROR", "message": "invalid relay job"})
    roadmap = loaded.get("roadmap", {})
    if isinstance(roadmap, dict):
        for p in roadmap.get("projects", []):
            if isinstance(p, dict):
                roadmaps.append({"id": clean(p.get("id")), "title": clean(p.get("title")),
                    "stages": [{"id": clean(s.get("id")), "title": clean(s.get("title")),
                                "task_ids": [str(x) for x in s.get("task_ids", []) if isinstance(x, (str, int))],
                                "observed_state": clean(s.get("observed_state")),
                                "acceptance_scope": clean(s.get("acceptance_scope")),
                                "acceptance_conditions": clean(s.get("acceptance_conditions"))}
                               for s in p.get("stages", []) if isinstance(s, dict)]})
    return {"tasks": tasks, "diagnostics": diagnostics, "projects": roadmaps, "freshness_threshold_seconds": threshold,
            "polled_at": dt.datetime.now(dt.timezone.utc).isoformat()}

def handler(config):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            route = urllib.parse.urlsplit(self.path)
            routes = {"/": ("task-board.html", "text/html; charset=utf-8"),
                      "/task-board.js": ("task-board.js", "text/javascript; charset=utf-8"),
                      "/task-board.css": ("task-board.css", "text/css; charset=utf-8")}
            if route.path == "/api/status" and not route.query:
                try: self.respond(200, json.dumps(project(config), ensure_ascii=False).encode(), "application/json; charset=utf-8")
                except Exception: self.respond(503, b'{"error":"source projection failed"}', "application/json")
            elif route.path in routes and not route.query:
                name, mime = routes[route.path]
                self.respond(200, (HERE / name).read_bytes(), mime)
            else: self.respond(404, b"Not found", "text/plain")
        def do_POST(self): self.respond(405, b"Read only", "text/plain")
        do_PUT = do_DELETE = do_PATCH = do_POST
        def log_message(self, *args): pass
    return Handler

def serve(config, host="127.0.0.1", port=8781):
    if host not in ("127.0.0.1", "::1", "localhost"): raise ValueError("loopback only")
    server = ThreadingHTTPServer((host, port), handler(config))
    return server

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8781)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"): parser.error("loopback only")
    config = read_json(args.config)
    if not isinstance(config, dict): parser.error("config must be object")
    with serve(config, args.host, args.port) as server: server.serve_forever()

if __name__ == "__main__": main()
