"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliations (
    reconcile_id   TEXT NOT NULL,
    effective_day  TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(effective_day, run_id)
);
CREATE TABLE IF NOT EXISTS signoffs (
    reconcile_id   TEXT PRIMARY KEY,
    effective_day  TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    note           TEXT NOT NULL,
    signed_at      TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_recon_day ON reconciliations(effective_day);
CREATE INDEX IF NOT EXISTS idx_signoff_day ON signoffs(effective_day);
"""


def db_path() -> Path:
    raw = os.environ.get("METRICGRAPH_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "reconciliations", "signoffs"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 生效日选批 ----------

def all_runs() -> list[dict]:
    """按生效起点排序的全部批次，选批与复核都以这份清单为准。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY effective_from, version_seq"
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def save_reconciliation(record: dict) -> None:
    """同一天同一批次的复核只留一行：重跑走 UPSERT，不重复登记。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO reconciliations (reconcile_id, effective_day, run_id,
                   selection_json, payload_json, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(effective_day, run_id) DO UPDATE SET
                   reconcile_id=excluded.reconcile_id,
                   selection_json=excluded.selection_json,
                   payload_json=excluded.payload_json,
                   created_at=excluded.created_at""",
            (
                record["reconcile_id"], record["effective_day"], record["run_id"],
                _dumps(record["selection"]), _dumps(record), record["created_at"],
            ),
        )


def find_reconciliation(effective_day: str, run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reconciliations WHERE effective_day=? AND run_id=?",
            (effective_day, run_id),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def reconciliation_by_id(reconcile_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reconciliations WHERE reconcile_id=? LIMIT 1",
            (reconcile_id,),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_reconciliations(limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM reconciliations
               ORDER BY effective_day DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


# ---------- 签收 ----------

def save_signoff(record: dict) -> None:
    """签收键就是复核键；重复签收更新备注与时间，但不换批次、不新增行。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO signoffs (reconcile_id, effective_day, run_id, note,
                   signed_at, payload_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(reconcile_id) DO UPDATE SET
                   note=excluded.note, signed_at=excluded.signed_at,
                   payload_json=excluded.payload_json""",
            (
                record["reconcile_id"], record["effective_day"], record["run_id"],
                record["note"], record["signed_at"], _dumps(record),
            ),
        )


def signoff_by_id(reconcile_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM signoffs WHERE reconcile_id=?", (reconcile_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def signoff_for(effective_day: str, run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM signoffs WHERE effective_day=? AND run_id=?",
            (effective_day, run_id),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def day_signoff(effective_day: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM signoffs WHERE effective_day=? LIMIT 1",
            (effective_day,),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_signoffs(limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM signoffs ORDER BY signed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]
