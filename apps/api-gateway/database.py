"""
CCDT — SQLite Database Layer
════════════════════════════════════════════════════════════════════════════════
Single SQLite file at /data/ccdt.db (Docker volume: ccdt-data).

Tables:
  incidents       — every incident with full timeline JSON
  opa_policies    — all OPA policies including LLM-authored ones
  chaos_runs      — every chaos engineering scenario result
  guardian_actions — every action executed by the Guardian

Usage:
    from database import db
    db.save_incident(incident_dict)
    incidents = db.list_incidents(status="active")
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("ccdt.database")

DB_PATH = os.getenv("SQLITE_PATH", "/data/ccdt.db")


def _ensure_dir() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warning',
    status      TEXT NOT NULL DEFAULT 'active',
    type        TEXT NOT NULL DEFAULT 'fault',
    node        TEXT,
    root_cause  TEXT,
    affected    TEXT DEFAULT '[]',
    confidence  REAL DEFAULT 0.0,
    auto_action TEXT DEFAULT '',
    timeline    TEXT DEFAULT '[]',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    resolved_at INTEGER,
    mttr_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS opa_policies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    rego_code     TEXT NOT NULL,
    description   TEXT,
    source        TEXT NOT NULL DEFAULT 'builtin',
    status        TEXT NOT NULL DEFAULT 'pending',
    approved_by   TEXT,
    approved_at   INTEGER,
    created_at    INTEGER NOT NULL,
    triggered_by  TEXT,
    effectiveness REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS chaos_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id     TEXT NOT NULL,
    scenario_title  TEXT NOT NULL,
    type            TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    resolved_at     INTEGER,
    mttr_seconds    INTEGER,
    guardian_action TEXT,
    action_success  INTEGER DEFAULT 0,
    incident_id     TEXT
);

CREATE TABLE IF NOT EXISTS guardian_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_name TEXT NOT NULL,
    target      TEXT NOT NULL,
    parameters  TEXT DEFAULT '{}',
    opa_result  TEXT DEFAULT 'allow',
    ghost_risk  REAL DEFAULT 0.0,
    executed    INTEGER DEFAULT 0,
    dry_run     INTEGER DEFAULT 0,
    detail      TEXT,
    incident_id TEXT,
    executed_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_status    ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_created   ON incidents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chaos_runs_started  ON chaos_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_executed_at ON guardian_actions(executed_at DESC);
"""


def init_db() -> None:
    """Create all tables and seed built-in OPA policies."""
    with get_db() as conn:
        conn.executescript(SCHEMA)

    # Seed built-in policies if table is empty
    _seed_builtin_policies()
    logger.info("SQLite database initialised at %s", DB_PATH)


def _seed_builtin_policies() -> None:
    builtin = [
        {
            "name": "no_privilege_escalation",
            "description": "Block any action that grants CAP_SYS_ADMIN or root privileges",
            "source": "builtin",
            "status": "active",
            "rego_code": open("/app/opa/policies/no_privilege_escalation.rego").read()
                         if os.path.exists("/app/opa/policies/no_privilege_escalation.rego")
                         else "# builtin policy — see opa/policies/",
        },
        {
            "name": "cpu_threshold",
            "description": "Prevent CPU throttling below 10% to avoid starvation",
            "source": "builtin",
            "status": "active",
            "rego_code": "# builtin — cpu_threshold.rego",
        },
        {
            "name": "egress_control",
            "description": "Block egress to unknown external IPs",
            "source": "builtin",
            "status": "active",
            "rego_code": "# builtin — egress_control.rego",
        },
        {
            "name": "lateral_movement",
            "description": "Block cross-namespace API calls",
            "source": "builtin",
            "status": "active",
            "rego_code": "# builtin — lateral_movement.rego",
        },
        {
            "name": "oom_notification",
            "description": "Require SRE notification before memory limit changes",
            "source": "builtin",
            "status": "active",
            "rego_code": "# builtin — oom_notification.rego",
        },
    ]
    now = int(time.time())
    with get_db() as conn:
        for p in builtin:
            conn.execute("""
                INSERT OR IGNORE INTO opa_policies
                    (name, description, source, status, rego_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (p["name"], p["description"], p["source"], p["status"],
                  p["rego_code"], now))


# ── Incident CRUD ─────────────────────────────────────────────────────────────

class Database:
    """High-level database API used by all routers."""

    # ── Incidents ─────────────────────────────────────────────────────────────

    def save_incident(self, inc: dict) -> None:
        now = int(time.time())
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO incidents
                    (id, title, severity, status, type, node, root_cause,
                     affected, confidence, auto_action, timeline,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                inc["id"],
                inc.get("title", ""),
                inc.get("severity", "warning"),
                inc.get("status", "active"),
                inc.get("type", "fault"),
                inc.get("node", ""),
                inc.get("rootCause", ""),
                json.dumps(inc.get("affected", [])),
                float(inc.get("confidence", 0)),
                inc.get("autoAction", ""),
                json.dumps(inc.get("timeline", [])),
                inc.get("createdAt", now),
                now,
            ))

    def get_incident(self, inc_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (inc_id,)
            ).fetchone()
        return _row_to_incident(row) if row else None

    def list_incidents(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query  = "SELECT * FROM incidents"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_incident(r) for r in rows]

    def update_incident_status(self, inc_id: str, status: str) -> None:
        now = int(time.time())
        resolved_at = now if status in ("resolved", "auto-resolved") else None
        with get_db() as conn:
            if resolved_at:
                row = conn.execute(
                    "SELECT created_at FROM incidents WHERE id = ?", (inc_id,)
                ).fetchone()
                mttr = resolved_at - row["created_at"] if row else None
                conn.execute("""
                    UPDATE incidents
                    SET status=?, updated_at=?, resolved_at=?, mttr_seconds=?
                    WHERE id=?
                """, (status, now, resolved_at, mttr, inc_id))
            else:
                conn.execute(
                    "UPDATE incidents SET status=?, updated_at=? WHERE id=?",
                    (status, now, inc_id)
                )

    def append_timeline(self, inc_id: str, event: str, icon: str = "📌") -> None:
        now = int(time.time())
        inc = self.get_incident(inc_id)
        if not inc:
            return
        import datetime
        ts = datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S")
        timeline = inc.get("timeline", [])
        timeline.append({"time": ts, "event": event, "icon": icon})
        with get_db() as conn:
            conn.execute(
                "UPDATE incidents SET timeline=?, updated_at=? WHERE id=?",
                (json.dumps(timeline), now, inc_id)
            )

    def get_summary(self) -> dict:
        with get_db() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            active  = conn.execute("SELECT COUNT(*) FROM incidents WHERE status='active'").fetchone()[0]
            avg_mttr= conn.execute(
                "SELECT AVG(mttr_seconds) FROM incidents WHERE mttr_seconds IS NOT NULL"
            ).fetchone()[0]
            by_sev  = conn.execute(
                "SELECT severity, COUNT(*) as cnt FROM incidents GROUP BY severity"
            ).fetchall()
        return {
            "total": total,
            "active": active,
            "avg_mttr_seconds": round(avg_mttr or 0, 1),
            "by_severity": {r["severity"]: r["cnt"] for r in by_sev},
        }

    # ── OPA Policies ─────────────────────────────────────────────────────────

    def save_policy(self, policy: dict) -> int:
        now = int(time.time())
        with get_db() as conn:
            cur = conn.execute("""
                INSERT OR REPLACE INTO opa_policies
                    (name, rego_code, description, source, status,
                     triggered_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                policy["name"],
                policy["rego_code"],
                policy.get("description", ""),
                policy.get("source", "llm"),
                policy.get("status", "pending"),
                policy.get("triggered_by", ""),
                now,
            ))
            return cur.lastrowid

    def approve_policy(self, policy_id: int, approved_by: str = "operator") -> bool:
        now = int(time.time())
        with get_db() as conn:
            conn.execute("""
                UPDATE opa_policies
                SET status='active', approved_by=?, approved_at=?
                WHERE id=?
            """, (approved_by, now, policy_id))
        return True

    def list_policies(self, status: str | None = None) -> list[dict]:
        query  = "SELECT * FROM opa_policies"
        params: list = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_policy(self, policy_id: int) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM opa_policies WHERE id=?", (policy_id,)
            ).fetchone()
        return dict(row) if row else None

    # ── Chaos runs ────────────────────────────────────────────────────────────

    def save_chaos_run(self, run: dict) -> int:
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO chaos_runs
                    (scenario_id, scenario_title, type, started_at,
                     resolved_at, mttr_seconds, guardian_action,
                     action_success, incident_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run["scenario_id"],
                run["scenario_title"],
                run.get("type", "fault"),
                run["started_at"],
                run.get("resolved_at"),
                run.get("mttr_seconds"),
                run.get("guardian_action", ""),
                int(run.get("action_success", False)),
                run.get("incident_id", ""),
            ))
            return cur.lastrowid

    def list_chaos_runs(self, limit: int = 100) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM chaos_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_chaos_stats(self) -> dict:
        with get_db() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM chaos_runs").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM chaos_runs WHERE action_success=1"
            ).fetchone()[0]
            avg_mttr = conn.execute(
                "SELECT AVG(mttr_seconds) FROM chaos_runs WHERE mttr_seconds IS NOT NULL"
            ).fetchone()[0]
        return {
            "total_runs":        total,
            "successful_remediations": success,
            "success_rate":      round(success / total * 100, 1) if total else 0,
            "avg_mttr_seconds":  round(avg_mttr or 0, 1),
        }

    # ── Guardian actions ──────────────────────────────────────────────────────

    def save_action(self, action: dict) -> None:
        now = int(time.time())
        with get_db() as conn:
            conn.execute("""
                INSERT INTO guardian_actions
                    (action_name, target, parameters, opa_result,
                     ghost_risk, executed, dry_run, detail,
                     incident_id, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                action["action_name"],
                action.get("target", ""),
                json.dumps(action.get("parameters", {})),
                action.get("opa_result", "allow"),
                float(action.get("ghost_risk", 0)),
                int(action.get("executed", False)),
                int(action.get("dry_run", False)),
                action.get("detail", ""),
                action.get("incident_id", ""),
                now,
            ))


# ── Row deserialiser ──────────────────────────────────────────────────────────

def _row_to_incident(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["affected"]  = json.loads(d.get("affected",  "[]"))
    d["timeline"]  = json.loads(d.get("timeline",  "[]"))
    d["rootCause"] = d.pop("root_cause", "")
    d["autoAction"]= d.pop("auto_action", "")
    d["createdAt"] = d.pop("created_at", 0)
    d["updatedAt"] = d.pop("updated_at", 0)
    return d


# ── Singleton ─────────────────────────────────────────────────────────────────

db = Database()
