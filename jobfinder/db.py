"""SQLite storage. Every job ever seen is kept (rejected ones without their description), so re-runs
never re-fetch, re-score or re-notify. Status flow:
    pending -> new -> queued -> applying -> applied
                 |        |         +-> ready_for_review -> applied      (filled form parked for approval)
                 |        |         +-> needs_input -> queued            (agent needs an answer from you)
                 |        |         +-> failed
                 +-> shortlisted / in_progress (manual "Apply now") / skipped / rejected
"""
import json
import sqlite3
import time
from contextlib import closing

import config
from .models import Job, dedupe_key, job_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE, dedupe_key TEXT,
    source TEXT, external_id TEXT, url TEXT, company TEXT, title TEXT, location TEXT,
    description TEXT, posted_at TEXT, first_seen TEXT,
    work_mode TEXT, exp_min REAL, exp_max REAL, salary_min_lpa REAL, salary_max_lpa REAL,
    location_ok INTEGER, score INTEGER, reasons TEXT DEFAULT '[]', red_flags TEXT DEFAULT '[]',
    status TEXT, reject_reason TEXT DEFAULT '', notes TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0, attempted_at TEXT, applied_at TEXT,
    apply_summary TEXT DEFAULT '', apply_run_dir TEXT DEFAULT '', pending_question TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT, source TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY (day, source)
);
CREATE TABLE IF NOT EXISTS source_runs (name TEXT PRIMARY KEY, last_run REAL);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, score);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.JOBS_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _exec(sql: str, params=()):
    """One statement per connection (threads never share one). SELECTs return list[dict]; others the rowid."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else cur.lastrowid


_NEW_COLUMNS = {  # added after the first release: older jobs.db files are upgraded in place
    "attempts": "INTEGER DEFAULT 0", "attempted_at": "TEXT", "applied_at": "TEXT",
    "apply_summary": "TEXT DEFAULT ''", "apply_run_dir": "TEXT DEFAULT ''", "pending_question": "TEXT DEFAULT ''",
}


def init() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for col, ddl in _NEW_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")


def _chunks(items: list, n: int = 500):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _existing(column: str, values: list) -> set:
    found = set()
    for chunk in _chunks(list(values)):
        marks = ",".join("?" * len(chunk))
        found |= {r[column] for r in _exec(f"SELECT {column} FROM jobs WHERE {column} IN ({marks})", chunk)}
    return found


def known_keys(keys: list) -> set:
    return _existing("key", keys)


def known_dedupe(keys: list) -> set:
    """Roles already seen elsewhere. Ones we only dropped for being too old don't count, so a reposted
    role with a fresh date is still considered."""
    found = set()
    for chunk in _chunks(list(keys)):
        marks = ",".join("?" * len(chunk))
        found |= {r["dedupe_key"] for r in _exec(
            f"SELECT dedupe_key FROM jobs WHERE dedupe_key IN ({marks}) AND reject_reason != 'too old'", chunk)}
    return found


def insert_job(job: Job, status: str, reason: str = "") -> None:
    _exec(
        """INSERT OR IGNORE INTO jobs
           (key, dedupe_key, source, external_id, url, company, title, location, description, posted_at,
            first_seen, work_mode, exp_min, exp_max, salary_min_lpa, salary_max_lpa, status, reject_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_key(job), dedupe_key(job), job.source, job.external_id, job.url, job.company, job.title,
         job.location, "" if status == "rejected" else job.description[:8000], job.posted_at,
         time.strftime("%Y-%m-%d %H:%M:%S"), job.work_mode, job.exp_min, job.exp_max,
         job.salary_min_lpa, job.salary_max_lpa, status, reason))


def pending(limit: int) -> list:
    return _exec("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT ?", (limit,))


def pending_count() -> int:
    return _exec("SELECT COUNT(*) AS n FROM jobs WHERE status='pending'")[0]["n"]


def save_eval(job_id: int, fields: dict, status: str, reason: str = "") -> None:
    _exec(
        """UPDATE jobs SET work_mode=?, exp_min=?, exp_max=?, salary_min_lpa=?, salary_max_lpa=?,
           location_ok=?, score=?, reasons=?, red_flags=?, status=?, reject_reason=? WHERE id=?""",
        (fields["work_mode"], fields["exp_min"], fields["exp_max"], fields["salary_min_lpa"],
         fields["salary_max_lpa"], int(fields["location_ok"]), fields["score"],
         json.dumps(fields["reasons"]), json.dumps(fields["red_flags"]), status, reason, job_id))


def get(job_id: int) -> dict | None:
    rows = _exec("SELECT * FROM jobs WHERE id=?", (job_id,))
    return rows[0] if rows else None


def set_status(job_id: int, status: str, reset_attempts: bool = False) -> None:
    _exec("""UPDATE jobs SET status=?, attempts=CASE WHEN ? THEN 0 ELSE attempts END,
             applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END WHERE id=?""",
          (status, int(reset_attempts), status, _now(), job_id))


def list_jobs(statuses: list, min_score: int = 0, q: str = "", limit: int = 200) -> list:
    where, params = [f"status IN ({','.join('?' * len(statuses))})"], list(statuses)
    if min_score:
        where.append("COALESCE(score, 0) >= ?")
        params.append(min_score)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        params += [f"%{q}%"] * 3
    params.append(limit)
    return _exec(
        f"""SELECT id, source, url, company, title, location, posted_at, first_seen, work_mode, exp_min, exp_max,
                   salary_min_lpa, salary_max_lpa, score, reasons, red_flags, status, reject_reason,
                   attempts, applied_at, apply_summary, pending_question
            FROM jobs WHERE {' AND '.join(where)}
            ORDER BY COALESCE(score, -1) DESC, id DESC LIMIT ?""", params)


# ---- autopilot queue ----

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today_like() -> str:
    return time.strftime("%Y-%m-%d") + "%"


def queue_new_matches(min_score: int, sources: list) -> int:
    """Promote shortlist-worthy 'new' jobs from the allowed sources to 'queued' for the autopilot."""
    if not sources:
        return 0
    marks = ",".join("?" * len(sources))
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            f"UPDATE jobs SET status='queued' WHERE status='new' AND COALESCE(score,0) >= ? AND source IN ({marks})",
            [min_score, *sources])
        return cur.rowcount


def next_to_apply(max_attempts: int, company_days: int = 30) -> dict | None:
    """Best queued job. Skips a company we applied to recently (or are already working on)."""
    rows = _exec(
        """SELECT * FROM jobs j WHERE j.status='queued' AND j.attempts < ? AND j.url LIKE 'http%'
           AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.company = j.company AND o.id != j.id AND
                (o.status IN ('applying','ready_for_review','in_progress')
                 OR (o.status='applied' AND o.applied_at > date('now', ?))))
           ORDER BY COALESCE(j.score, 0) DESC, j.id LIMIT 1""",
        (max_attempts, f"-{company_days} day"))
    return rows[0] if rows else None


def mark_attempt(job_id: int) -> None:
    _exec("UPDATE jobs SET status='applying', attempts=attempts+1, attempted_at=? WHERE id=?", (_now(), job_id))


def set_apply_result(job_id: int, status: str, summary: str, run_dir=None, question: dict | None = None) -> None:
    _exec("""UPDATE jobs SET status=?, apply_summary=?, apply_run_dir=COALESCE(?, apply_run_dir),
             pending_question=?, applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END WHERE id=?""",
          (status, (summary or "")[:1000], str(run_dir) if run_dir else None,
           json.dumps(question) if question else "", status, _now(), job_id))


def attempts_today() -> int:
    return _exec("SELECT COUNT(*) AS n FROM jobs WHERE attempted_at LIKE ?", (_today_like(),))[0]["n"]


def submits_today() -> int:
    return _exec("SELECT COUNT(*) AS n FROM jobs WHERE status='applied' AND applied_at LIKE ?",
                 (_today_like(),))[0]["n"]


def reset_stale() -> None:
    """After a restart no browser tab survives: anything mid-flight goes back to the queue."""
    _exec("UPDATE jobs SET status='queued' WHERE status IN ('applying','ready_for_review')")


# ---- request budgets (persisted, so restarts can't reset the counters) ----

def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def record_call(source: str) -> None:
    _exec("""INSERT INTO api_usage(day, source, calls) VALUES(?, ?, 1)
             ON CONFLICT(day, source) DO UPDATE SET calls = calls + 1""", (_day(), source))


def calls_today(source: str) -> int:
    rows = _exec("SELECT calls FROM api_usage WHERE day=? AND source=?", (_day(), source))
    return rows[0]["calls"] if rows else 0


def calls_this_month(source: str) -> int:
    prefix = time.strftime("%Y-%m", time.gmtime()) + "-%"
    return _exec("SELECT COALESCE(SUM(calls), 0) AS n FROM api_usage WHERE source=? AND day LIKE ?",
                 (source, prefix))[0]["n"]


def usage(source: str = "adzuna") -> dict:
    return {"today": calls_today(source), "month": calls_this_month(source)}


def hours_since(name: str) -> float | None:
    rows = _exec("SELECT last_run FROM source_runs WHERE name=?", (name,))
    return (time.time() - rows[0]["last_run"]) / 3600 if rows else None


def mark_run(name: str) -> None:
    _exec("""INSERT INTO source_runs(name, last_run) VALUES(?, ?)
             ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run""", (name, time.time()))


def counts() -> dict:
    return {r["status"]: r["n"] for r in _exec("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
