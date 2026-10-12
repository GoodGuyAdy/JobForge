"""Autopilot: feeds the finder's queued jobs into the apply agent, one at a time, unattended.

AUTOPILOT_MODE
  off     nothing happens automatically
  review  the agent fills each application and PARKS it (its tab stays open) until you approve it
  auto    the agent also submits, but only when every gate passes; anything doubtful is parked instead

Unattended means nobody can answer questions. When the agent needs an answer it doesn't have (or a login /
CAPTCHA), that job moves to 'needs_input' with the question; you answer once (it is remembered for every
future form) and the job is re-queued.

All browser work runs on the worker thread (Playwright's sync API is thread-bound); the HTTP side only
reads state and enqueues approve/discard requests.
"""
import json
import random
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from ats import detect as detect_ats, get_handler as get_ats_handler
from jobfinder import db
from store import new_run_dir, save_answer, save_result

from .browser import (
    begin_job,
    close_job_pages,
    current_page,
    navigate_adzuna_flow,
    resume_job,
    snapshot,
)
from .core import APPLY_TASK, SUBMIT_TASK, Hooks, run_agent

_LOCK = threading.Lock()
STATE = {"paused": False, "failures": 0, "next_at": 0.0}


# ----------------------------------------------------------------------------
# State and cheap checks (safe to call from any thread)
# ----------------------------------------------------------------------------

def enabled() -> bool:
    return config.AUTOPILOT_MODE in ("review", "auto")


def is_paused() -> bool:
    with _LOCK:
        return STATE["paused"]


def pause() -> None:
    with _LOCK:
        STATE["paused"] = True


def resume() -> None:
    with _LOCK:
        STATE.update(paused=False, failures=0, next_at=0.0)


def backoff(seconds: float) -> None:
    """Hold the autopilot off for a while (used after an unexpected worker error)."""
    with _LOCK:
        STATE["next_at"] = max(STATE["next_at"], time.time() + seconds)


def ready(parked_count: int = 0) -> bool:
    """Is there work the autopilot may do right now? (No browser needed to answer.)"""
    with _LOCK:
        if not enabled() or STATE["paused"] or time.time() < STATE["next_at"]:
            return False
    if db.attempts_today() >= config.AUTO_MAX_ATTEMPTS_PER_DAY or parked_count >= config.AUTO_MAX_PARKED:
        return False
    return db.next_to_apply(config.AUTO_MAX_ATTEMPTS_PER_JOB) is not None


def status() -> dict:
    with _LOCK:
        paused, failures = STATE["paused"], STATE["failures"]
    counts = db.counts()
    return {
        "mode": config.AUTOPILOT_MODE, "paused": paused, "failures": failures,
        "attempts_today": db.attempts_today(), "max_attempts": config.AUTO_MAX_ATTEMPTS_PER_DAY,
        "submits_today": db.submits_today(), "max_submits": config.AUTO_MAX_SUBMITS_PER_DAY,
        "queued": counts.get("queued", 0), "ready_for_review": counts.get("ready_for_review", 0),
        "needs_input": counts.get("needs_input", 0),
    }


# ----------------------------------------------------------------------------
# Worker-thread logic
# ----------------------------------------------------------------------------

def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _unattended_hooks(stop, emit, asked: dict) -> Hooks:
    def ask(question, choices):
        asked.update(question=question, choices=choices)
        return None  # nobody to ask: the agent ends this attempt with status needs_input

    return Hooks(ask=ask, stopped=stop.is_set, emit=emit)


def _submit(sess, hooks, run_dir) -> dict:
    sess.allow_submit = True
    try:
        return run_agent(sess, SUBMIT_TASK, hooks, run_dir, max_steps=8)
    finally:
        sess.allow_submit = False


def _auto_gate(job: dict, result: dict) -> tuple:
    """May this filled application be submitted without a human? Returns (ok, reason if not)."""
    if config.AUTOPILOT_MODE != "auto":
        return False, "review mode"
    if (job.get("score") or 0) < config.AUTO_SUBMIT_MIN_SCORE:
        return False, f"fit score below {config.AUTO_SUBMIT_MIN_SCORE}"
    if result.get("review_notes"):
        return False, "the agent flagged answers to double-check"
    if result.get("blank_required"):
        return False, "required fields still blank"
    if db.submits_today() >= config.AUTO_MAX_SUBMITS_PER_DAY:
        return False, "daily submit cap reached"
    return True, ""


def _park(sess, job: dict, result: dict, run_dir, why: str, emit) -> None:
    """Leave the filled form open in its own tab and queue it for human approval."""
    try:
        snapshot(sess, path=run_dir / "review.jpg", full_page=True)
    except Exception:
        pass
    sess.parked[job["id"]] = list(sess.job_pages)
    sess.job_pages = []  # detach, so the next job's tab handling never touches these
    notes = result.get("review_notes") or []
    summary = (result.get("summary") or "") + (f" Check: {'; '.join(notes)}." if notes else "") + f" (Parked: {why}.)"
    db.set_apply_result(job["id"], "ready_for_review", summary, run_dir)
    emit(f"AUTOPILOT: ready for your review: {job['title']} @ {job['company']} ({why})")


def _finish(sess, job: dict, result: dict, asked: dict, run_dir, hooks, emit) -> str:
    """Translate the agent's result into a job status. Returns applied|parked|needs_input|stopped|failed."""
    status_, jid = result["status"], job["id"]
    save_result(run_dir, result)

    if status_ == "ready_for_review":
        ok, why = _auto_gate(job, result)
        if ok:
            emit("AUTOPILOT: all gates passed, submitting")
            sub = _submit(sess, hooks, run_dir)
            if sub["status"] == "confirmation_seen":
                db.set_apply_result(jid, "applied", sub["summary"], run_dir)
                close_job_pages(sess)
                emit(f"AUTOPILOT: APPLIED {job['title']} @ {job['company']}")
                return "applied"
            why = f"auto-submit did not complete: {sub['summary']}"
        _park(sess, job, result, run_dir, why, emit)
        return "parked"

    if status_ == "confirmation_seen":
        db.set_apply_result(jid, "applied", result["summary"], run_dir)
        close_job_pages(sess)
        emit(f"AUTOPILOT: APPLIED {job['title']} @ {job['company']}")
        return "applied"

    if status_ == "needs_input":
        db.set_apply_result(jid, "needs_input", result["summary"], run_dir,
                            question={"question": asked.get("question") or result.get("question", ""),
                                      "choices": asked.get("choices") or result.get("choices") or []})
        close_job_pages(sess)
        emit(f"AUTOPILOT: needs your answer: {result['summary']}")
        return "needs_input"

    if status_ == "stopped":
        db.set_apply_result(jid, "queued", "Stopped before finishing.", run_dir)
        close_job_pages(sess)
        return "stopped"

    # cannot_continue / error: retry later, give up after AUTO_MAX_ATTEMPTS_PER_JOB
    final = "failed" if job["attempts"] + 1 >= config.AUTO_MAX_ATTEMPTS_PER_JOB else "queued"
    db.set_apply_result(jid, final, result["summary"], run_dir)
    close_job_pages(sess)
    emit(f"AUTOPILOT: {final}: {job['title']} @ {job['company']}: {result['summary']}")
    return "failed"


def tick(sess, emit, stop, set_status) -> bool:
    """Process at most one queued job. Returns True if it did any work."""
    if not ready(len(sess.parked)):
        return False
    job = db.next_to_apply(config.AUTO_MAX_ATTEMPTS_PER_JOB)
    if not job:
        return False

    stop.clear()
    set_status("running")
    db.mark_attempt(job["id"])
    run_dir = new_run_dir(job["url"])
    label = f"{job['title']} @ {job['company']}"
    emit(f"AUTOPILOT: applying to {label} (score {job['score']}, {config.AUTOPILOT_MODE} mode)")
    asked: dict = {}
    hooks = _unattended_hooks(stop, emit, asked)

    try:
        begin_job(sess)
        page = current_page(sess)
        page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
        _settle(page)
        navigate_adzuna_flow(page)

        # ── ATS detection & routing ──────────────────────────────────────
        # Re-check the URL after Adzuna/aggregator redirect has resolved.
        final_url = current_page(sess).url
        ats_name = detect_ats(final_url)
        task = f"{APPLY_TASK} The job is: {label}."

        if ats_name:
            handler = get_ats_handler(final_url)
            emit(f"AUTOPILOT: ATS detected = {ats_name} — using deterministic handler")
            result = handler.run(sess, hooks, run_dir, task)
        else:
            emit("AUTOPILOT: ATS unknown — using AI agent")
            result = run_agent(sess, task, hooks, run_dir)
        # ── End ATS routing ──────────────────────────────────────────────

        outcome = _finish(sess, job, result, asked, run_dir, hooks, emit)
    except Exception as e:
        result = {"status": "error", "summary": f"{type(e).__name__}: {e}"[:300]}
        try:
            outcome = _finish(sess, job, result, asked, run_dir, hooks, emit)
        except Exception:
            outcome = "failed"

    paused_now = False
    with _LOCK:
        if outcome != "stopped":
            STATE["failures"] = 0 if outcome in ("applied", "parked", "needs_input") else STATE["failures"] + 1
        STATE["next_at"] = time.time() + config.AUTO_COOLDOWN_SECONDS * random.uniform(0.75, 1.5)
        if STATE["failures"] >= config.AUTO_FAILURE_PAUSE:
            STATE["paused"] = paused_now = True
    if paused_now:
        emit(f"AUTOPILOT PAUSED after {config.AUTO_FAILURE_PAUSE} consecutive failures. "
             "Check the latest run in runs/, then press Resume on the Jobs page.")
    set_status("idle")
    return True


def approve_parked(sess, job_id: int, emit, stop, set_status) -> None:
    """The human approved a parked application: switch to its tab and submit."""
    stop.clear()
    job = db.get(job_id)
    pages = sess.parked.pop(job_id, None)
    if not job:
        return
    run_dir = Path(job["apply_run_dir"]) if job.get("apply_run_dir") else new_run_dir(job["url"])
    if not pages or all(p.is_closed() for p in pages):
        db.set_apply_result(job_id, "queued", "The review tab was closed, so the application will be redone.", run_dir)
        return
    set_status("submitting")
    emit(f"AUTOPILOT: submitting approved application: {job['title']} @ {job['company']}")
    resume_job(sess, pages)
    sub = _submit(sess, _unattended_hooks(stop, emit, {}), run_dir)
    if sub["status"] == "confirmation_seen":
        db.set_apply_result(job_id, "applied", sub["summary"], run_dir)
        close_job_pages(sess, pages)
        emit(f"AUTOPILOT: APPLIED {job['title']} @ {job['company']}")
    else:
        sess.parked[job_id] = pages  # keep the tab so you can look at it
        db.set_apply_result(job_id, "ready_for_review", f"Submit did not complete: {sub['summary']}", run_dir)
        emit(f"AUTOPILOT: submit did not complete for {job['title']} @ {job['company']}: {sub['summary']}")
    sess.job_pages = []
    set_status("idle")


def discard_parked(sess, job_id: int, emit) -> None:
    pages = sess.parked.pop(job_id, None) or []
    close_job_pages(sess, pages)
    db.set_status(job_id, "skipped")
    emit(f"AUTOPILOT: discarded application {job_id}")


# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------

class AnswerBody(BaseModel):
    answer: str


class ModeBody(BaseModel):
    mode: str  # off | review | auto


def make_router(enqueue) -> APIRouter:
    """enqueue(job_dict) hands work to the browser worker thread."""
    r = APIRouter()

    @r.get("/api/autopilot")
    def get_status():
        db.init()
        return status()

    @r.post("/api/autopilot/mode")
    def set_mode(body: ModeBody):
        mode = body.mode.strip().lower()
        if mode not in ("off", "review", "auto"):
            raise HTTPException(400, "mode must be off, review or auto")
        config.set_autopilot_mode(mode)
        if mode != "off":
            resume()  # flipping the mode on is an explicit go-ahead: don't stay paused from an earlier run
        return status()

    @r.post("/api/autopilot/pause")
    def do_pause():
        pause()
        return {"ok": True}

    @r.post("/api/autopilot/resume")
    def do_resume():
        resume()
        return {"ok": True}

    def _need(job_id: int, expected: str) -> dict:
        job = db.get(job_id)
        if not job:
            raise HTTPException(404)
        if job["status"] != expected:
            raise HTTPException(409, f"Job is '{job['status']}', not '{expected}'")
        return job

    @r.post("/api/autopilot/{job_id}/approve")
    def approve(job_id: int):
        _need(job_id, "ready_for_review")
        db.set_status(job_id, "applying")  # blocks double-clicks; the worker sets the final status
        enqueue({"kind": "approve_parked", "job_id": job_id})
        return {"ok": True}

    @r.post("/api/autopilot/{job_id}/discard")
    def discard(job_id: int):
        _need(job_id, "ready_for_review")
        enqueue({"kind": "discard_parked", "job_id": job_id})
        return {"ok": True}

    @r.post("/api/autopilot/{job_id}/answer")
    def answer(job_id: int, body: AnswerBody):
        job = _need(job_id, "needs_input")
        q = json.loads(job.get("pending_question") or "{}").get("question", "")
        if not q or not body.answer.strip():
            raise HTTPException(400, "No pending question or empty answer")
        save_answer(q, body.answer)  # remembered: used by every future application
        db.set_apply_result(job_id, "queued", "Answer saved; re-queued.", None)
        db.set_status(job_id, "queued", reset_attempts=True)
        return {"ok": True}

    @r.get("/api/autopilot/{job_id}/review.jpg")
    def review_image(job_id: int):
        job = db.get(job_id)
        path = Path(job["apply_run_dir"]) / "review.jpg" if job and job.get("apply_run_dir") else None
        if not path or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    return r
