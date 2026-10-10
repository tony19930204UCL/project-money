"""Cron-oriented, PAPER-only market session scheduler."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os

from cio_market_lab.engine.paper_session import PaperSession
from cio_market_lab.engine.pnl_attribution import attribute_pnl
from cio_market_lab.engine.strategy_feedback import update_weights, load_table, save_table

MARKETS = {"TW": ("Asia/Taipei", time(9), time(13, 30)),
           "US": ("America/New_York", time(9, 30), time(16))}


def _clear_stale_lock(lock, max_age):
    """A killed run leaves its lock behind; a lock older than max_age is dead."""
    import time as _t
    try:
        if _t.time() - Path(lock).stat().st_mtime > max_age:
            Path(lock).unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def run_daily(now, config):
    """Run configured sessions and process each market's close exactly once.

    config requires markets mapping; each market supplies a session_factory
    callable returning PaperSession, ticks, holidays and optional feedback
    weight_table_path. Runtime dependencies are injected for offline tests.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    root = Path(config.get("report_dir", "data/paper_reports"))
    root.mkdir(parents=True, exist_ok=True)
    lock = Path(config.get("lock_path", root / ".paper_scheduler.lock"))
    _clear_stale_lock(lock, config.get("lock_max_age_seconds", 9 * 3600))
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"status": "LOCKED", "markets": {}}
    try:
        os.close(fd)
        results = {}
        for market, settings in config.get("markets", {}).items():
            if market not in MARKETS:
                raise ValueError("unsupported market: " + str(market))
            zone, opening, closing = MARKETS[market]
            local = now.astimezone(ZoneInfo(zone))
            day = local.date().isoformat()
            if local.weekday() >= 5 or day in settings.get("holidays", []):
                results[market] = "SKIPPED_CALENDAR"
                continue
            session_key = root / (day + "-" + market + ".session.json")
            report = root / (day + ".json")
            if opening <= local.time().replace(tzinfo=None) < closing:
                if session_key.exists():
                    results[market] = "ALREADY_RAN"
                    continue
                factory = settings["session_factory"]
                session = factory()
                if not isinstance(session, PaperSession) and not hasattr(session, "run_session"):
                    raise TypeError("session_factory must return a session")
                summary = session.run_session(settings.get("ticks", 1))
                _atomic_json(session_key, {"summary": summary, "feedback_done": False})
                results[market] = "SESSION_RAN"
            elif local.time().replace(tzinfo=None) >= closing:
                if not session_key.exists():
                    results[market] = "NO_SESSION"
                    continue
                state = json.loads(session_key.read_text(encoding="utf-8"))
                if state.get("feedback_done"):
                    results[market] = "ALREADY_PROCESSED"
                    continue
                journal_path = settings["journal_path"]
                journal = [json.loads(line) for line in Path(journal_path).read_text(encoding="utf-8").splitlines() if line.strip()] if Path(journal_path).exists() else []
                attribution = attribute_pnl(journal, settings.get("closed_trades", []), as_of=day)
                changes = ""
                table_path = settings.get("weight_table_path")
                if table_path:
                    table = load_table(table_path)
                    updated, changes = update_weights(attribution, table, evidence_id=market + ":" + day)
                    save_table(table_path, updated)
                payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"date": day, "paper_only": True, "markets": {}}
                payload["markets"][market] = {"session": state["summary"], "attribution": attribution, "weight_changes": changes}
                _atomic_json(report, payload)
                (root / (day + ".md")).write_text(
                    "# Paper trading — " + day + "\n\n" +
                    "\n\n".join(k + ": " + v["attribution"]["daily_markdown"] + "\n" + v["weight_changes"] for k, v in payload["markets"].items()) + "\n",
                    encoding="utf-8")
                state["feedback_done"] = True
                _atomic_json(session_key, state)
                results[market] = "FEEDBACK_DONE"
            else:
                results[market] = "BEFORE_OPEN"
        return {"status": "OK", "markets": results}
    finally:
        lock.unlink(missing_ok=True)


def _atomic_json(path, data):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(data, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
