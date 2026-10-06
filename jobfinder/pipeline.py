"""Orchestrates one search run and keeps its state for the UI.

fetch -> dedupe -> cheap filters -> (store) -> LLM scoring -> post filters -> shortlist -> alert
"""
import concurrent.futures as cf
import threading
import time

import httpx

import config
from . import db, filters, rank, settings, sources
from .models import Criteria, Job, dedupe_key, job_key

_LOCK = threading.Lock()
_CONTROL = threading.Condition(_LOCK)
_STATE = {"running": False, "paused": False, "cancel_requested": False, "log": [], "summary": None}


def emit(line: str) -> None:
    with _LOCK:
        _STATE["log"].append(f"{time.strftime('%H:%M:%S')} {line}")
        del _STATE["log"][:-200]


def run_state() -> dict:
    with _LOCK:
        return {key: _STATE[key] for key in ("running", "paused", "cancel_requested", "summary")} | {
            "log": list(_STATE["log"])}


def pause_run() -> bool:
    with _CONTROL:
        if not _STATE["running"] or _STATE["cancel_requested"]:
            return False
        _STATE["paused"] = True
        return True


def resume_run() -> bool:
    with _CONTROL:
        if not _STATE["running"] or not _STATE["paused"] or _STATE["cancel_requested"]:
            return False
        _STATE["paused"] = False
        _CONTROL.notify_all()
        return True


def cancel_run() -> bool:
    with _CONTROL:
        if not _STATE["running"]:
            return False
        _STATE["cancel_requested"] = True
        _STATE["paused"] = False
        _CONTROL.notify_all()
        return True


def _continue_search() -> bool:
    with _CONTROL:
        while _STATE["running"] and _STATE["paused"] and not _STATE["cancel_requested"]:
            _CONTROL.wait()
        return not _STATE["cancel_requested"]


def run_search(c: Criteria, log=emit, force: bool = False, should_continue=None) -> dict:
    should_continue = should_continue or (lambda: True)
    db.init()
    with sources.client() as http:
        found = sources.fetch_all(http, c, settings.load_companies(), log, force,
                                  should_continue=should_continue)

    # dedupe against everything already seen, and within this batch
    known_k = db.known_keys([job_key(j) for j in found])
    known_d = db.known_dedupe([dedupe_key(j) for j in found])
    fresh, seen = [], set()
    for j in found:
        d = dedupe_key(j)
        if job_key(j) in known_k or d in known_d or d in seen:
            continue
        seen.add(d)
        fresh.append(j)

    hard_rejected = 0
    for j in fresh:
        reason = filters.hard_filter(j, c)
        hard_rejected += bool(reason)
        db.insert_job(j, "rejected" if reason else "pending", reason or "")
    log(f"fetched {len(found)}, {len(fresh)} unseen, {hard_rejected} rejected by cheap filters")

    if not should_continue():
        log("search cancelled; fetched jobs were saved, scoring was skipped")
        return {"fetched": len(found), "unseen": len(fresh), "hard_rejected": hard_rejected,
                "new_matches": 0, "still_pending": db.pending_count(), "cancelled": True}

    matches = evaluate_pending(c, log, should_continue)
    if not should_continue():
        log("search cancelled; completed scores were saved")
        return {"fetched": len(found), "unseen": len(fresh), "hard_rejected": hard_rejected,
                "new_matches": len(matches), "still_pending": db.pending_count(), "cancelled": True}

    if config.AUTOPILOT_MODE in ("review", "auto"):
        queued = db.queue_new_matches(max(config.AUTO_QUEUE_MIN_SCORE, c.min_score), config.AUTO_SOURCES)
        if queued:
            log(f"autopilot: queued {queued} jobs for application")
    left = db.pending_count()
    summary = {"fetched": len(found), "unseen": len(fresh), "hard_rejected": hard_rejected,
               "new_matches": len(matches), "still_pending": left}
    log(f"done: {len(matches)} new matches" + (f", {left} jobs waiting for the next run (LLM cap)" if left else ""))
    notify(matches)
    return summary


def evaluate_pending(c: Criteria, log, should_continue=None) -> list:
    should_continue = should_continue or (lambda: True)
    rows = db.pending(config.MAX_LLM_PER_RUN)
    if not rows:
        return []
    log(f"scoring {len(rows)} jobs with {config.JOBS_MODEL}")
    instructions = rank.build_instructions(c)
    return _score_rows(rows, instructions, c, log, should_continue)


def _score_rows(rows: list, instructions: str, c: Criteria, log, should_continue) -> list:
    def work(row):
        if not should_continue():
            return row, None
        try:
            return row, rank.evaluate(instructions, row)
        except Exception as e:
            return row, e

    matches = []
    with cf.ThreadPoolExecutor(4) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        while futures:
            if not should_continue():
                for future in futures:
                    future.cancel()
                break
            done, _ = cf.wait(futures, timeout=0.25, return_when=cf.FIRST_COMPLETED)
            for future in done:
                futures.pop(future)
                row, ev = future.result()
                _save_evaluation(row, ev, c, log, matches)
    return sorted(matches, key=lambda m: -m["score"])


def _save_evaluation(row: dict, ev, c: Criteria, log, matches: list) -> None:
    if ev is None:
        return
    if isinstance(ev, Exception):
        log(f"scoring failed for {row['title']!r} ({e_name(ev)}); will retry next run")
        return
    job = Job(**{k: row[k] for k in Job.model_fields})
    fields = rank.merge(job, ev)
    judged = job.model_copy(update={k: fields[k] for k in
                                    ("work_mode", "exp_min", "exp_max", "salary_min_lpa", "salary_max_lpa")})
    reason = filters.post_filter(judged, c, fields["score"], fields["location_ok"])
    db.save_eval(row["id"], fields, "rejected" if reason else "new", reason or "")
    if not reason:
        matches.append({**fields, "title": row["title"], "company": row["company"], "url": row["url"]})


def e_name(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:120]}"


def notify(matches: list) -> None:
    """Optional Telegram alert for new matches."""
    if not (matches and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    lines = [f"{m['score']} | {m['title']} @ {m['company']} ({m['work_mode'] or '?'})\n{m['url']}"
             for m in matches[:10]]
    try:
        httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage", timeout=15,
                   json={"chat_id": config.TELEGRAM_CHAT_ID, "disable_web_page_preview": True,
                         "text": f"{len(matches)} new job matches:\n\n" + "\n\n".join(lines)})
    except httpx.HTTPError as e:
        emit(f"Telegram alert failed: {e}")


def start_run_async(c: Criteria, force: bool = False) -> bool:
    """Run in a background thread. Returns False if a run is already in progress."""
    with _CONTROL:
        if _STATE["running"]:
            return False
        _STATE.update(running=True, paused=False, cancel_requested=False, log=[], summary=None)

    def target():
        try:
            summary = run_search(c, force=force, should_continue=_continue_search)
        except Exception as e:
            emit(f"ERROR: {e_name(e)}")
            summary = None
        with _CONTROL:
            _STATE.update(running=False, paused=False, summary=summary)
            _CONTROL.notify_all()

    threading.Thread(target=target, daemon=True).start()
    return True


def start_scheduler() -> threading.Thread:
    """Runs for the lifetime of the app. Searches on the saved criteria whenever auto-search is enabled
    (config.JOBS_AUTO_RUN_HOURS > 0), re-reading that setting every tick so turning it on/off or changing
    the interval from the Jobs page takes effect immediately, without a restart."""
    def loop():
        last_run = 0.0
        while True:
            hours = config.JOBS_AUTO_RUN_HOURS
            if hours > 0 and time.time() - last_run >= hours * 3600:
                start_run_async(settings.load_criteria())  # skipped if a manual run is in progress
                last_run = time.time()
            time.sleep(30)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
