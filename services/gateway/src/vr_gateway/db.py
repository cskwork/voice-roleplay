"""SQLite store (PROTOCOL §9). Transcripts/feedback are only written for opt-in sessions/attempts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, mode TEXT NOT NULL, scenario_id TEXT NOT NULL, scenario_version TEXT,
  difficulty TEXT, history_opt_in INTEGER NOT NULL, state TEXT NOT NULL,
  created_at REAL NOT NULL, ended_at REAL, summary_json TEXT
);
CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_index INTEGER NOT NULL, role TEXT NOT NULL,
  transcript_revision INTEGER, text TEXT, spoken_segments_json TEXT, playback_status TEXT, metrics_json TEXT,
  created_at REAL NOT NULL, UNIQUE (session_id, turn_index)
);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY, session_id TEXT, exercise_type TEXT NOT NULL, scenario_id TEXT,
  exercise_ref TEXT, attempt_index INTEGER NOT NULL, history_opt_in INTEGER NOT NULL, result_version INTEGER,
  metrics_json TEXT, target_diff_json TEXT, extra_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_revisions (
  owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, revision INTEGER NOT NULL, text TEXT NOT NULL,
  source TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (owner_type, owner_id, revision)
);
CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
  transcript_revision INTEGER, item_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
  idempotency_key TEXT UNIQUE, error_code TEXT, transcript_revision INTEGER,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL
);
CREATE TABLE IF NOT EXISTS review_items (
  item_id TEXT PRIMARY KEY, text_en TEXT NOT NULL, text_ko TEXT, source_type TEXT, source_id TEXT,
  box INTEGER NOT NULL DEFAULT 1, due_at REAL NOT NULL, last_result TEXT, created_at REAL NOT NULL,
  UNIQUE (text_en, source_id)
);
CREATE TABLE IF NOT EXISTS model_manifest (
  model_id TEXT PRIMARY KEY, revision TEXT, sha256 TEXT, runtime TEXT, license TEXT, benchmark_id TEXT
);
"""

TERMINAL_JOB_STATES = ("completed", "failed", "cancelled", "expired")
USER_DATA_TABLES = ("turns", "sessions", "transcript_revisions", "feedback", "attempts", "review_items")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # settings
    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key, value FROM settings")}

    def put_settings(self, values: dict) -> None:
        with self.tx() as c:
            for k, v in values.items():
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))

    # jobs (metadata only; never audio or text)
    def insert_job(self, job: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs (job_id, attempt_id, kind, state, idempotency_key, transcript_revision,"
                " created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job["job_id"], job["attempt_id"], job["kind"], job["state"], job["idempotency_key"],
                 job.get("transcript_revision"), job["created_at"], job["created_at"], job.get("expires_at")),
            )

    def job_by_key(self, key: str) -> dict | None:
        rows = self.query("SELECT * FROM jobs WHERE idempotency_key = ?", (key,))
        return rows[0] if rows else None

    def set_job_state(self, job_id: str, state: str, error_code: str | None = None, conn=None) -> None:
        sql = "UPDATE jobs SET state = ?, error_code = ?, updated_at = ? WHERE job_id = ?"
        args = (state, error_code, time.time(), job_id)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.tx() as c:
                c.execute(sql, args)

    def expire_unfinished_jobs(self) -> int:
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE jobs SET state = 'expired', error_code = 'AUDIO_EXPIRED', updated_at = ?"
                f" WHERE state NOT IN ({','.join('?' * len(TERMINAL_JOB_STATES))})",
                (time.time(), *TERMINAL_JOB_STATES),
            )
            return cur.rowcount

    # sessions / turns (opt-in only; callers check)
    def upsert_session(self, s: dict, conn=None) -> None:
        sql = (
            "INSERT INTO sessions (session_id, mode, scenario_id, scenario_version, difficulty, history_opt_in, state,"
            " created_at, ended_at, summary_json) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET state=excluded.state, ended_at=excluded.ended_at,"
            " summary_json=excluded.summary_json"
        )
        args = (s["session_id"], s["mode"], s["scenario_id"], s.get("scenario_version"), s.get("difficulty"),
                1, s["state"], s["created_at"], s.get("ended_at"),
                json.dumps(s["summary"], ensure_ascii=False) if s.get("summary") is not None else None)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.tx() as c:
                c.execute(sql, args)

    def upsert_turn(self, t: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO turns (turn_id, session_id, turn_index, role, transcript_revision, text,"
                " spoken_segments_json, playback_status, metrics_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(turn_id) DO UPDATE SET text=excluded.text, spoken_segments_json=excluded.spoken_segments_json,"
                " playback_status=excluded.playback_status, metrics_json=excluded.metrics_json",
                (t["turn_id"], t["session_id"], t["turn_index"], t["role"], t.get("transcript_revision"), t.get("text"),
                 _j(t.get("spoken_segments")), t.get("playback_status"), _j(t.get("metrics")), t.get("created_at", time.time())),
            )
            if t["role"] == "user" and t.get("text") is not None:
                c.execute(
                    "INSERT OR IGNORE INTO transcript_revisions VALUES ('turn', ?, ?, ?, 'asr', ?)",
                    (t["turn_id"], t.get("transcript_revision") or 1, t["text"], time.time()),
                )

    # attempts
    def upsert_attempt(self, a: dict, conn=None) -> None:
        sql = (
            "INSERT INTO attempts (attempt_id, session_id, exercise_type, scenario_id, exercise_ref, attempt_index,"
            " history_opt_in, result_version, metrics_json, target_diff_json, extra_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET"
            " result_version=excluded.result_version, metrics_json=excluded.metrics_json,"
            " target_diff_json=excluded.target_diff_json, extra_json=excluded.extra_json"
        )
        args = (a["attempt_id"], a.get("session_id"), a["exercise_type"], a.get("scenario_id"), a.get("exercise_ref"),
                a["attempt_index"], 1, a.get("result_version"), _j(a.get("metrics")), _j(a.get("target_diff")),
                _j(a.get("extra")), a["created_at"])
        if conn is not None:
            conn.execute(sql, args)
        else:
            self._exec_tx(sql, args)

    def _exec_tx(self, sql: str, args: tuple) -> None:
        with self.tx() as c:
            c.execute(sql, args)

    def insert_revision(self, owner_type: str, owner_id: str, revision: int, text: str, source: str, conn) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO transcript_revisions VALUES (?,?,?,?,?,?)",
            (owner_type, owner_id, revision, text, source, time.time()),
        )

    def insert_feedback(self, owner_type: str, owner_id: str, items: list[dict], conn) -> None:
        for item in items:
            conn.execute(
                "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?)",
                (item["feedback_id"], owner_type, owner_id, item.get("transcript_revision"),
                 json.dumps(item, ensure_ascii=False), time.time()),
            )

    # review (Leitner)
    def add_review_item(self, item: dict, conn=None) -> None:
        sql = (
            "INSERT OR IGNORE INTO review_items (item_id, text_en, text_ko, source_type, source_id, box, due_at, created_at)"
            " VALUES (?,?,?,?,?,1,?,?)"
        )
        now = time.time()
        args = (item["item_id"], item["text_en"], item.get("text_ko"), item.get("source_type"), item.get("source_id"), now, now)
        if conn is not None:
            conn.execute(sql, args)
        else:
            self._exec_tx(sql, args)

    def upsert_manifest(self, model_id: str, revision: str | None, runtime: str | None) -> None:
        self._exec_tx(
            "INSERT INTO model_manifest (model_id, revision, runtime) VALUES (?,?,?) ON CONFLICT(model_id)"
            " DO UPDATE SET revision=excluded.revision, runtime=excluded.runtime", (model_id, revision, runtime))

    # history
    def history(self) -> dict:
        sessions = self.query("SELECT * FROM sessions ORDER BY created_at DESC")
        for s in sessions:
            s["summary"] = json.loads(s.pop("summary_json") or "null")
            s["history_opt_in"] = bool(s["history_opt_in"])
            s["turns"] = [_turn(t) for t in self.query(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_index", (s["session_id"],))]
        attempts = self.query("SELECT * FROM attempts ORDER BY created_at DESC")
        for a in attempts:
            a["history_opt_in"] = bool(a["history_opt_in"])
            for key in ("metrics", "target_diff", "extra"):
                a[key] = json.loads(a.pop(f"{key}_json") or "null")
            a["revisions"] = self.query(
                "SELECT revision, text, source, created_at FROM transcript_revisions"
                " WHERE owner_type = 'attempt' AND owner_id = ? ORDER BY revision", (a["attempt_id"],))
            a["feedback"] = [json.loads(r["item_json"]) for r in self.query(
                "SELECT item_json FROM feedback WHERE owner_type = 'attempt' AND owner_id = ?"
                " ORDER BY transcript_revision, created_at", (a["attempt_id"],))]
        return {"sessions": sessions, "attempts": attempts}

    def delete_history(self) -> None:
        with self.tx() as c:
            for table in USER_DATA_TABLES:
                c.execute(f"DELETE FROM {table}")
            c.execute(f"DELETE FROM jobs WHERE state IN ({','.join('?' * len(TERMINAL_JOB_STATES))})", TERMINAL_JOB_STATES)
        with self._lock:
            self.conn.execute("VACUUM")


def _j(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _turn(t: dict) -> dict:
    t["spoken_segments"] = json.loads(t.pop("spoken_segments_json") or "null")
    t["metrics"] = json.loads(t.pop("metrics_json") or "null")
    return t
