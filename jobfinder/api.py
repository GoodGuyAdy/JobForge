"""HTTP API + page for job discovery. Mounted into app.py via make_router(start_apply)."""
import json
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import config
from . import db, pipeline, settings
from .models import Criteria

USER_STATUSES = {"new", "shortlisted", "queued", "in_progress", "applied", "skipped"}
LOCKED = {"applying", "ready_for_review"}  # owned by the autopilot; use its approve/discard endpoints


class StatusBody(BaseModel):
    status: str


class ScheduleBody(BaseModel):
    auto_run_hours: float


def _run_search_control(control: Callable[[], bool], detail: str) -> dict:
  if not control():
    raise HTTPException(status_code=409, detail=detail)
  return {"ok": True}


def _present(row: dict) -> dict:
    row["reasons"] = json.loads(row.get("reasons") or "[]")
    row["red_flags"] = json.loads(row.get("red_flags") or "[]")
    row["pending_question"] = json.loads(row["pending_question"]) if row.get("pending_question") else None
    return row


def make_router(start_apply: Callable[[str], None]) -> APIRouter:
    """start_apply(url) hands a job URL to the apply agent (raises HTTPException 409 if it is busy)."""
    r = APIRouter()

    @r.get("/api/jobs")
    def list_jobs(status: str = "new", min_score: int = 0, q: str = "", limit: int = 200):
        db.init()
        return {"jobs": [_present(x) for x in db.list_jobs(status.split(","), min_score, q, limit)],
                "counts": db.counts()}

    @r.get("/api/jobs/criteria")
    def get_criteria():
        return settings.load_criteria()

    @r.put("/api/jobs/criteria")
    def put_criteria(c: Criteria):
        settings.save_criteria(c)
        return {"ok": True}

    @r.post("/api/jobs/search")
    def search():
        if not pipeline.start_run_async(settings.load_criteria(), force=True):  # manual: skip cadence guards
            raise HTTPException(409, "A search is already running")
        return {"ok": True}

    @r.post("/api/jobs/run/pause", responses={409: {"description": "No active search to pause"}})
    def pause_search():
      return _run_search_control(pipeline.pause_run, "No active search to pause")

    @r.post("/api/jobs/run/resume", responses={409: {"description": "No paused search to resume"}})
    def resume_search():
      return _run_search_control(pipeline.resume_run, "No paused search to resume")

    @r.post("/api/jobs/run/cancel", responses={409: {"description": "No active search to cancel"}})
    def cancel_search():
      return _run_search_control(pipeline.cancel_run, "No active search to cancel")

    @r.get("/api/jobs/budget")
    def budget():
        db.init()
        return {"adzuna": {**db.usage("adzuna"), "max_day": config.ADZUNA_MAX_PER_DAY,
                           "max_month": config.ADZUNA_MAX_PER_MONTH}}

    @r.get("/api/jobs/run")
    def run():
        return pipeline.run_state()

    @r.get("/api/jobs/schedule")
    def get_schedule():
        return {"auto_run_hours": config.JOBS_AUTO_RUN_HOURS}

    @r.post("/api/jobs/schedule")
    def set_schedule(body: ScheduleBody):
        if body.auto_run_hours < 0:
            raise HTTPException(400, "auto_run_hours must be >= 0")
        config.set_auto_run_hours(body.auto_run_hours)
        return {"ok": True, "auto_run_hours": config.JOBS_AUTO_RUN_HOURS}

    @r.post("/api/jobs/{job_id}/status")
    def set_status(job_id: int, body: StatusBody):
        if body.status not in USER_STATUSES:
            raise HTTPException(400, "bad status")
        row = db.get(job_id)
        if not row:
            raise HTTPException(404)
        if row["status"] in LOCKED:
            raise HTTPException(409, "Use Approve or Discard for applications waiting on the autopilot")
        db.set_status(job_id, body.status, reset_attempts=(body.status == "queued"))
        return {"ok": True}

    @r.post("/api/jobs/{job_id}/apply")
    def apply(job_id: int):
        row = db.get(job_id)
        if not row:
            raise HTTPException(404)
        start_apply(row["url"])  # raises 409 if the agent is busy
        db.set_status(job_id, "in_progress")
        return {"ok": True}

    @r.get("/jobs", response_class=HTMLResponse)
    def page():
        return HTMLResponse(PAGE)

    return r


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Find jobs</title>
<style>
body{font-family:Arial,sans-serif;max-width:1150px;margin:30px auto;padding:0 20px;color:#222}
.box{border:1px solid #ccc;border-radius:6px;padding:14px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.grid label{font-size:13px;display:flex;flex-direction:column;gap:3px}
input[type=text],input[type=number]{padding:7px}
button{padding:8px 14px;margin:4px 4px 4px 0;cursor:pointer}
.tab{background:#eee;border:1px solid #ccc;border-radius:14px}.tab.on{background:#222;color:#fff}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid #eee;padding:8px;text-align:left;vertical-align:top}
.score{display:inline-block;min-width:34px;text-align:center;border-radius:4px;color:#fff;padding:2px 6px;font-weight:bold}
pre{background:#f5f5f5;padding:10px;font-size:12px;max-height:200px;overflow:auto;white-space:pre-wrap}
.small{font-size:12px;color:#555}.flag{color:#a33}
</style></head><body>
<p><a href="/">&larr; Apply agent</a></p>
<h1>Find jobs</h1>

<div class="box"><h3>Search criteria</h3>
<div class="grid">
 <label>Job titles (comma separated)<input type="text" id="titles" placeholder="AI engineer, ML engineer"></label>
 <label>Locations (empty = whole country)<input type="text" id="locations" placeholder="Bengaluru, Hyderabad"></label>
 <label>Country<input type="text" id="country"></label>
 <label>Work mode<span>
   <input type="checkbox" id="m_remote"> remote &nbsp;<input type="checkbox" id="m_hybrid"> hybrid &nbsp;
   <input type="checkbox" id="m_onsite"> onsite</span></label>
 <label>Required experience: min years<input type="number" id="min_exp" step="0.5"></label>
 <label>Required experience: max years<input type="number" id="max_exp" step="0.5"></label>
 <label>Min salary (LPA)<input type="number" id="min_salary" step="0.5"></label>
 <label><span><input type="checkbox" id="unk_salary"> include jobs with no salary listed</span></label>
 <label>Must mention one of (keywords)<input type="text" id="include_kw"></label>
 <label>Exclude title words<input type="text" id="exclude_kw"></label>
 <label>Exclude companies<input type="text" id="exclude_co"></label>
 <label>Posted within (days)<input type="number" id="age"></label>
 <label>Min fit score (0-100)<input type="number" id="min_score"></label>
 <label>Sources<span><input type="checkbox" id="s_companies"> company boards &nbsp;
   <input type="checkbox" id="s_adzuna"> Adzuna &nbsp;
   <input type="checkbox" id="s_remoteok"> RemoteOK &nbsp;
   <input type="checkbox" id="s_jobicy"> Jobicy</span></label>
</div>
<button onclick="save().then(()=>msg('Saved'))">Save</button>
<button onclick="saveAndSearch()">Save &amp; search now</button>
<span id="runstate" class="small"></span> <span id="budget" class="small"></span>
<button id="pauseSearch" hidden onclick="controlSearch('pause')">Pause search</button>
<button id="resumeSearch" hidden onclick="controlSearch('resume')">Resume search</button>
<button id="cancelSearch" hidden onclick="controlSearch('cancel')">Cancel search</button>
<p class="small">Company boards come from <code>companies.json</code>. Experience filters match against the years
the posting REQUIRES (overlap with your min&ndash;max). Re-running only scores new postings.</p>
</div>

<div class="box" id="ap"><b>Automation</b>
 <div class="grid">
  <label>Autopilot mode<select id="mode">
    <option value="off">Off &ndash; apply manually only</option>
    <option value="review">Review &ndash; fill forms, park for my approval</option>
    <option value="auto">Auto &ndash; fill and submit automatically</option>
  </select></label>
  <label>Auto-search every (hours, 0 = off)<input type="number" id="autorun" step="0.5" min="0"></label>
 </div>
 <button onclick="saveAutomation()">Save automation settings</button>
 <span id="apstate" class="small"></span>
 <button id="apbtn" hidden></button>
 <p class="small">When autopilot is Review/Auto, queued jobs (score &ge; the min above, company-board sources)
 are applied to automatically in the background &mdash; no need to click anything per job. Auto-search keeps
 fetching new postings on the interval above using the saved criteria.</p>
</div>

<pre id="log" hidden></pre>
<div id="tabs"></div>
<p id="notice" class="small"></p>
<table><thead><tr><th>Score</th><th>Role</th><th>Details</th><th>Why</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>

<script>
const $ = (id) => document.getElementById(id);
const csv = (s) => s.split(',').map(x => x.trim()).filter(Boolean);
const num = (s) => s === '' ? null : Number(s);
const msg = (t) => { $('notice').textContent = t; };
const api = (method, path, body) => fetch(path, {method, headers: {'Content-Type': 'application/json'},
  body: body ? JSON.stringify(body) : undefined}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { msg(j.detail || 'Request failed'); throw new Error(j.detail); }
    return j; });

const TABS = [['new','New'],['queued','Queued'],['ready_for_review','Ready for review'],
              ['needs_input','Needs input'],['in_progress','In progress'],['applied','Applied'],
              ['failed','Failed'],['shortlisted','Shortlisted'],['skipped','Skipped'],['rejected','Filtered out']];
let tab = 'new';

function fill(c) {
  $('titles').value = c.titles.join(', '); $('locations').value = c.locations.join(', ');
  $('country').value = c.country;
  for (const m of ['remote','hybrid','onsite']) $('m_' + m).checked = c.work_modes.includes(m);
  $('min_exp').value = c.min_exp ?? ''; $('max_exp').value = c.max_exp ?? '';
  $('min_salary').value = c.min_salary_lpa ?? ''; $('unk_salary').checked = c.include_unknown_salary;
  $('include_kw').value = c.include_keywords.join(', '); $('exclude_kw').value = c.exclude_keywords.join(', ');
  $('exclude_co').value = c.exclude_companies.join(', '); $('age').value = c.max_age_days;
  $('min_score').value = c.min_score;
  for (const s of ['companies','adzuna','remoteok','jobicy']) $('s_' + s).checked = c.sources.includes(s);
}
function collect() {
  return {
    titles: csv($('titles').value), locations: csv($('locations').value), country: $('country').value.trim() || 'India',
    work_modes: ['remote','hybrid','onsite'].filter(m => $('m_' + m).checked),
    min_exp: num($('min_exp').value), max_exp: num($('max_exp').value), min_salary_lpa: num($('min_salary').value),
    include_unknown_salary: $('unk_salary').checked, include_keywords: csv($('include_kw').value),
    exclude_keywords: csv($('exclude_kw').value), exclude_companies: csv($('exclude_co').value),
    max_age_days: Number($('age').value) || 14, min_score: Number($('min_score').value) || 0,
    sources: ['companies','adzuna','remoteok','jobicy'].filter(s => $('s_' + s).checked),
  };
}
const save = () => api('PUT', '/api/jobs/criteria', collect());
async function saveAndSearch() { await save(); await api('POST', '/api/jobs/search'); msg('Search started...'); pollRun(); }
async function controlSearch(action) {
  await api('POST', `/api/jobs/run/${action}`);
  msg(action === 'cancel' ? 'Cancelling search...' : `Search ${action}d.`);
  pollRun();
}

function el(tag, props, ...kids) {
  const n = Object.assign(document.createElement(tag), props || {});
  kids.flat().forEach(k => n.append(k));
  return n;
}
const safeUrl = (u) => /^https?:\\/\\//i.test(u) ? u : '#';
const color = (s) => s >= 80 ? '#2a7' : s >= 60 ? '#e6a100' : '#999';
const fmt = (a, b, unit) => a == null && b == null ? '' :
  (a != null && b != null && a !== b ? `${a}-${b}` : `${a ?? b}${a != null && b == null ? '+' : ''}`) + unit;

function actions(j) {
  const st = j.status;
  const setStatus = (status) => () => api('POST', `/api/jobs/${j.id}/status`, {status}).then(load);
  const post = (path, note) => () => api('POST', path).then(() => { if (note) msg(note); load(); });
  const b = (label, fn) => el('button', {textContent: label, onclick: fn});
  const out = [];
  if (['new', 'shortlisted', 'skipped'].includes(st)) {
    out.push(b('Queue for autopilot', setStatus('queued')));
    out.push(b('Apply now', post(`/api/jobs/${j.id}/apply`, 'Sent to the apply agent. Watch it on the dashboard.')));
  }
  if (st === 'new') out.push(b('Shortlist', setStatus('shortlisted')));
  if (st === 'queued') out.push(b('Unqueue', setStatus('new')));
  if (st === 'ready_for_review') {
    out.push(b('Approve & submit', () => confirm('Submit this application now?') &&
      post(`/api/autopilot/${j.id}/approve`, 'Submitting...')()));
    out.push(el('a', {href: `/api/autopilot/${j.id}/review.jpg`, target: '_blank', rel: 'noopener', textContent: 'Screenshot '}));
    out.push(b('Discard', post(`/api/autopilot/${j.id}/discard`)));
  }
  if (st === 'failed') out.push(b('Retry', setStatus('queued')));
  if (st === 'in_progress') out.push(b('Mark applied', setStatus('applied')));
  if (!['skipped', 'applied', 'rejected', 'ready_for_review'].includes(st)) out.push(b('Skip', setStatus('skipped')));
  if (st === 'skipped') out.push(b('Restore', setStatus('new')));
  return out;
}

function answerBox(j) {
  const q = j.pending_question;
  if (!q) return [];
  const input = el('input', {type: 'text', placeholder: 'Your answer (remembered for future forms)'});
  const send = (v) => api('POST', `/api/autopilot/${j.id}/answer`, {answer: v}).then(load);
  return [el('div', {className: 'small', textContent: 'Question: ' + q.question}),
    ...(q.choices || []).map(c => el('button', {textContent: c, onclick: () => send(c)})),
    input, el('button', {textContent: 'Save & requeue', onclick: () => input.value && send(input.value)})];
}

function row(j) {
  const note = (cls, text) => el('div', {className: cls, textContent: text});
  const why = j.status === 'rejected' ? [note('small', j.reject_reason)] : [
    ...(j.apply_summary ? [note('small', j.apply_summary)] : []),
    ...(j.status === 'needs_input' ? answerBox(j) : []),
    ...j.reasons.map(r => note('small', '+ ' + r)),
    ...j.red_flags.map(r => note('small flag', '! ' + r))];
  const details = [j.work_mode, fmt(j.exp_min, j.exp_max, ' yrs'), fmt(j.salary_min_lpa, j.salary_max_lpa, ' LPA'),
                   j.location].filter(Boolean).join(' · ');
  return el('tr', {},
    el('td', {}, el('span', {className: 'score', textContent: j.score ?? '-', style: `background:${color(j.score ?? 0)}`})),
    el('td', {}, el('a', {href: safeUrl(j.url), target: '_blank', rel: 'noopener noreferrer',
                          textContent: j.title}), el('div', {className: 'small', textContent: `${j.company} · ${j.source}`})),
    el('td', {className: 'small', textContent: details}),
    el('td', {}, why),
    el('td', {}, actions(j)));
}

async function apLoad() {
  const a = await api('GET', '/api/autopilot');
  const btn = $('apbtn');
  if (document.activeElement !== $('mode')) $('mode').value = a.mode;  // don't fight the user mid-edit
  if (a.mode === 'off') {
    $('apstate').textContent = 'Autopilot is off: applying is manual only (use "Apply now" per job).';
    btn.hidden = true; return;
  }
  $('apstate').textContent = `${a.mode} mode · ${a.paused ? 'PAUSED' : 'running'} · applications today ` +
    `${a.attempts_today}/${a.max_attempts} · submitted ${a.submits_today}/${a.max_submits} · queued ${a.queued}` +
    (a.failures ? ` · ${a.failures} failures in a row` : '');
  btn.hidden = false; btn.textContent = a.paused ? 'Resume' : 'Pause';
  btn.onclick = () => api('POST', a.paused ? '/api/autopilot/resume' : '/api/autopilot/pause').then(apLoad);
}

async function loadAutomation() {
  const s = await api('GET', '/api/jobs/schedule');
  if (document.activeElement !== $('autorun')) $('autorun').value = s.auto_run_hours;
}

async function saveAutomation() {
  await api('POST', '/api/autopilot/mode', {mode: $('mode').value});
  await api('POST', '/api/jobs/schedule', {auto_run_hours: Number($('autorun').value) || 0});
  msg('Automation settings saved');
  apLoad(); loadAutomation();
}

async function load() {
  apLoad().catch(() => {});
  api('GET', '/api/jobs/budget').then(b => { $('budget').textContent =
    `Adzuna calls: ${b.adzuna.today}/${b.adzuna.max_day} today, ${b.adzuna.month}/${b.adzuna.max_month} this month`; });
  const d = await api('GET', `/api/jobs?status=${tab}`);
  $('tabs').replaceChildren(...TABS.map(([k, label]) => el('button', {
    className: 'tab' + (k === tab ? ' on' : ''), textContent: `${label} (${d.counts[k] || 0})`,
    onclick: () => { tab = k; load(); }})));
  $('rows').replaceChildren(...d.jobs.map(row));
}

let polling = false;
async function pollRun() {
  if (polling) return; polling = true;
  while (true) {
    const s = await api('GET', '/api/jobs/run');
    $('log').hidden = s.log.length === 0; $('log').textContent = s.log.join('\\n');
    $('runstate').textContent = !s.running ? '' : s.cancel_requested ? 'cancelling...' :
      s.paused ? 'paused' : 'searching...';
    $('pauseSearch').hidden = !s.running || s.paused || s.cancel_requested;
    $('resumeSearch').hidden = !s.running || !s.paused || s.cancel_requested;
    $('cancelSearch').hidden = !s.running || s.cancel_requested;
    if (!s.running) {
      if (s.summary?.cancelled) msg('Search cancelled; fetched jobs were saved and unscored jobs remain pending.');
      load(); break;
    }
    await new Promise(r => setTimeout(r, 1500));
  }
  polling = false;
}

api('GET', '/api/jobs/criteria').then(fill); loadAutomation(); load(); pollRun();
setInterval(() => { if (!document.hidden) { load(); loadAutomation(); pollRun(); } }, 5000);
</script></body></html>
"""
