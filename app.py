"""AutoApply AI: dashboard + job finder + autopilot, in one process.

- The job finder searches on a schedule (JOBS_AUTO_RUN_HOURS) and stores scored jobs in jobs.db.
- With AUTOPILOT_MODE=review|auto, good jobs are queued and the apply agent works through them unattended.
- Playwright's sync API must stay on the thread that created it, so ALL browser work happens in
  `worker_loop`; FastAPI handlers only queue jobs and read shared status.

Run:  uvicorn app:app --port 8000        (no --reload: it would restart the browser)
"""
import asyncio
import queue
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

import config
from agent import APPLY_TASK, SUBMIT_TASK, Hooks, run_agent
from agent import autopilot
from agent.browser import Session, current_page, navigate_adzuna_flow, snapshot
from jobfinder import db as jobs_db
from jobfinder.api import make_router
from jobfinder.pipeline import start_scheduler
from store import new_run_dir, save_result

# ----------------------------------------------------------------------------
# Shared status (written by the worker thread, read by HTTP handlers)
# ----------------------------------------------------------------------------


@dataclass
class Shared:
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "idle"  # idle | running | waiting_human | ready_for_review | submitting | done | error
    log: list = field(default_factory=list)
    question: dict = None
    answer: str = None
    answer_event: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    result: dict = None
    url: str = ""
    run_dir: object = None


S = Shared()
jobs: "queue.Queue[dict]" = queue.Queue()
BUSY = ("running", "waiting_human", "submitting")


def emit(line: str) -> None:
    with S.lock:
        S.log.append(line)
        del S.log[:-300]


def set_status(status: str) -> None:
    with S.lock:
        S.status = status


def ask_human(question: str, choices: list) -> str:
    with S.lock:
        S.question = {"text": question, "choices": choices}
        S.answer = None
        S.answer_event.clear()
        S.status = "waiting_human"
    emit(f"ASKING YOU: {question}")
    while not S.answer_event.wait(timeout=1):
        if S.stop.is_set():
            break
    with S.lock:
        answer = "" if S.stop.is_set() else (S.answer or "")
        S.question = None
        S.status = "running"
    return answer


HOOKS = Hooks(ask=ask_human, stopped=S.stop.is_set, emit=emit)

# ----------------------------------------------------------------------------
# Worker thread: owns Playwright
# ----------------------------------------------------------------------------


def open_session() -> Session:
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(config.USER_DATA_DIR), headless=config.HEADLESS, viewport=config.VIEWPORT)
    jobs_db.init()
    jobs_db.reset_stale()  # no tab survives a restart: anything mid-flight goes back to the queue
    return Session(context=ctx, docs=config.available_docs(), playwright=pw, seen=set(ctx.pages))


def finish_run(sess: Session, result: dict) -> None:
    save_result(S.run_dir, result)
    if result["status"] == "ready_for_review":
        snapshot(sess, path=S.run_dir / "review.jpg", full_page=True)
    with S.lock:
        S.result = result
        S.status = {"ready_for_review": "ready_for_review", "stopped": "idle"}.get(result["status"], "done")
    emit(f"FINISHED: {result['status']}: {result['summary']}")


def job_apply(sess: Session, url: str) -> None:
    S.stop.clear()
    with S.lock:
        S.status, S.result, S.url = "running", None, url
        S.log.clear()
    S.run_dir = new_run_dir(url)
    if not sess.docs:
        emit("WARNING: no documents found (put your resume at docs/resume.pdf); uploads will fail.")
    page = current_page(sess)
    if url:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        navigate_adzuna_flow(page)
    finish_run(sess, run_agent(sess, APPLY_TASK, HOOKS, S.run_dir))


def job_submit(sess: Session) -> None:
    S.stop.clear()
    set_status("submitting")
    sess.allow_submit = True
    try:
        finish_run(sess, run_agent(sess, SUBMIT_TASK, HOOKS, S.run_dir, max_steps=8))
    finally:
        sess.allow_submit = False


IDLE = ("idle", "done", "error")  # states in which the autopilot may start an application


def worker_loop() -> None:
    sess = None
    while True:
        try:
            job = jobs.get(timeout=15)
        except queue.Empty:
            # Nothing manual is waiting: let the autopilot work, unless a manual run needs the browser.
            if S.status in IDLE and autopilot.ready(len(sess.parked) if sess else 0):
                job = {"kind": "autopilot"}
            else:
                continue
        kind = job["kind"]
        if kind == "quit":
            if sess:
                sess.context.close()
                sess.playwright.stop()
            return
        try:
            if sess is None:
                sess = open_session()
            if kind == "apply":
                job_apply(sess, job.get("url", ""))
            elif kind == "submit":
                job_submit(sess)
            elif kind == "autopilot":
                autopilot.tick(sess, emit, S.stop, set_status)
            elif kind == "approve_parked":
                autopilot.approve_parked(sess, job["job_id"], emit, S.stop, set_status)
            elif kind == "discard_parked":
                autopilot.discard_parked(sess, job["job_id"], emit)
        except Exception as e:
            emit(f"ERROR: {type(e).__name__}: {e}")
            emit(traceback.format_exc()[-800:])
            set_status("error")
            if kind == "autopilot":
                autopilot.backoff(300)  # e.g. the browser would not start: don't retry in a tight loop
            elif kind == "approve_parked":
                jobs_db.set_apply_result(job["job_id"], "ready_for_review", f"Submit failed: {e}"[:300])


def start_apply(url: str) -> None:
    """Queue an application run (shared by the dashboard and the Jobs page)."""
    if S.status in BUSY:
        raise HTTPException(409, "A run is already in progress")
    set_status("running")
    jobs.put({"kind": "apply", "url": url.strip()})


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=worker_loop, daemon=True).start()
    start_scheduler()  # checks config.JOBS_AUTO_RUN_HOURS on its own; the Jobs page can turn it on/off live
    yield
    jobs.put({"kind": "quit"})


app = FastAPI(lifespan=lifespan)
app.include_router(make_router(start_apply))
app.include_router(autopilot.make_router(jobs.put))

# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------


class StartBody(BaseModel):
    url: str = ""


class AnswerBody(BaseModel):
    answer: str


@app.post("/api/start")
def api_start(body: StartBody):
    start_apply(body.url)
    return {"ok": True}


@app.post("/api/answer")
def api_answer(body: AnswerBody):
    if S.status != "waiting_human":
        raise HTTPException(409, "Nothing is waiting for an answer")
    S.answer = body.answer
    S.answer_event.set()
    return {"ok": True}


@app.post("/api/approve")
def api_approve():
    if S.status != "ready_for_review":
        raise HTTPException(409, "Nothing is ready for review")
    jobs.put({"kind": "submit"})
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    """Kill switch: halts the current run AND pauses the autopilot (resume it on the Jobs page)."""
    autopilot.pause()
    S.stop.set()
    S.answer_event.set()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    with S.lock:
        return {"status": S.status, "log": S.log[-80:], "question": S.question,
                "result": S.result, "url": S.url}


@app.get("/api/review.jpg")
def api_review():
    path = S.run_dir / "review.jpg" if S.run_dir else None
    if not path or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>AutoApply AI</title>
<style>
body{font-family:Arial,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#222}
input[type=text]{width:70%;padding:10px}button{padding:10px 16px;margin:4px 4px 4px 0;cursor:pointer}
pre{background:#f5f5f5;padding:12px;white-space:pre-wrap;max-height:340px;overflow:auto;font-size:12px}
.box{border:1px solid #ccc;border-radius:6px;padding:14px;margin:14px 0}
.ask{border-color:#e6a100;background:#fff8e6}.review{border-color:#2a7;background:#f0fff6}
#badge{display:inline-block;padding:2px 10px;border-radius:12px;background:#eee}
img{max-width:100%;border:1px solid #ddd}
</style></head><body>
<h1>AutoApply AI</h1>
<p><a href="/jobs">Find jobs &rarr;</a></p>
<p>Paste a job application URL, or leave it empty to work on the page already open in the browser
(e.g. after you logged in). <b>The agent never submits on its own: you approve the final click here.</b></p>
<input type="text" id="url" placeholder="https://...">
<button onclick="start()">Start</button><button onclick="stopRun()">Stop</button>
<p>Status: <span id="badge">idle</span></p>

<div id="ask" class="box ask" hidden>
  <b id="q"></b><div id="choices"></div>
  <input type="text" id="ans" placeholder="Your answer"><button onclick="answer()">Send</button>
  <button onclick="answer('done')">I did it (done)</button>
</div>

<div id="review" class="box review" hidden>
  <h3>Ready for your review</h3><pre id="summary"></pre>
  <p>Check the browser window (or the screenshot below), then:</p>
  <button onclick="approve()">Approve &amp; submit</button>
  <button onclick="start()">Run agent again on the current page</button>
  <br><img id="shot" alt="review screenshot">
</div>

<h3>Log</h3><pre id="log"></pre>
<script>
const $ = (id) => document.getElementById(id);
const post = (path, body) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(body || {})}).then(r => r.ok ? r.json() : r.json().then(e => alert(e.detail)));
const start = () => post('/api/start', {url: $('url').value});
const stopRun = () => post('/api/stop');
const approve = () => confirm('Submit this application now?') && post('/api/approve');
const answer = (v) => { post('/api/answer', {answer: v ?? $('ans').value}); $('ans').value = ''; };
let shotFor = null;
async function poll() {
  try {
    const s = await (await fetch('/api/status')).json();
    $('badge').textContent = s.status;
    $('log').textContent = s.log.join('\\n');
    $('ask').hidden = !s.question;
    if (s.question) {
      $('q').textContent = s.question.text;
      $('choices').replaceChildren(...(s.question.choices || []).map(c => {
        const b = document.createElement('button'); b.textContent = c; b.onclick = () => answer(c); return b; }));
    }
    $('review').hidden = s.status !== 'ready_for_review';
    if (s.status === 'ready_for_review' && s.result) {
      $('summary').textContent = [s.result.summary,
        s.result.review_notes?.length ? '\\nPlease double-check:\\n- ' + s.result.review_notes.join('\\n- ') : '',
        s.result.blank_required?.length ? '\\nStill blank: ' + s.result.blank_required.join(', ') : ''].join('\\n');
      if (shotFor !== s.result.summary) { shotFor = s.result.summary; $('shot').src = '/api/review.jpg?t=' + Date.now(); }
    }
  } catch (e) {}
  setTimeout(poll, 1500);
}
poll();
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(PAGE)
