# AutoApply AI: job finder + apply agent + autopilot

Two parts that share one dashboard:

1. **Job finder** (`jobfinder/`): fetch jobs -> dedupe -> cheap filters -> LLM fit score -> shortlist.
2. **Apply agent** (`agent.py`, `browser_tools.py`): opens a job in a visible browser, fills the form, and
   stops before submit. You approve the final click in the dashboard.

## Setup
    pip install -r requirements.txt
    playwright install chromium
    cp .env.example .env              # add OPENAI_API_KEY (and optional Adzuna keys)
    cp companies.example.json companies.json   # then edit: the companies whose boards you want polled
    mkdir docs && cp /path/to/resume.pdf docs/resume.pdf
    # profile: keep your PROFILE dict in user_profile.py (or profile.py)

  Optional: set `JOB_PORTAL_ACCOUNT_PASSWORD` in your local `.env` to let the agent fill job-portal account
  creation password and confirmation fields. The password stays local and is not sent to the model or written to
  run logs. Use a unique password; the user still reviews and submits the account form and handles verification.

## The autonomous loop
    finder (every JOBS_AUTO_RUN_HOURS) -> scored jobs in jobs.db -> 'queued' (score >= AUTO_QUEUE_MIN_SCORE,
    company-board sources only) -> autopilot applies one job at a time -> applied / parked / needs your input

One process does everything: `uvicorn app:app --port 8000`. Set the mode from the **Automation** box on the
Jobs page (`/jobs`) - it also lets you set the auto-search interval there. Both take effect immediately, no
restart needed (they're saved to `automation.json`, which overrides `.env` on the next start too):
- `off`    nothing automatic (default). Use the Jobs page buttons manually.
- `review` the agent fills each queued application and PARKS it: its tab stays open, you approve on the
           Jobs page ("Ready for review" tab: screenshot, Approve & submit, Discard).
- `auto`   the agent also submits, but only if ALL gates pass: fit score >= AUTO_SUBMIT_MIN_SCORE, no answers
           flagged for double-checking (e.g. drafted essays), no blank required fields, daily submit cap not hit.
           Anything doubtful is parked for review instead.
Recommended: run `review` for your first 15-20 applications, read what it filled in, then consider `auto`.


Unattended = nobody to answer questions. If the agent needs an answer it doesn't have (work authorisation,
a login, a CAPTCHA...), that job goes to the "Needs input" tab with the question. Answer once; the answer is
remembered for every future form and the job is re-queued.

Safety rails: daily caps on attempts and submits, randomised cooldown between applications, one application per
company per 30 days, max 2 attempts per job, auto-pause after 5 failures in a row, and a Stop button that halts
the current run and pauses the autopilot (Resume on the Jobs page). Do not also run `python -m jobfinder --watch`
alongside the app: the app already schedules the finder.

## Run
    uvicorn app:app --port 8000       # no --reload
Open http://localhost:8000
- `/` is the apply agent dashboard. `/jobs` is the job finder.
- On `/jobs`: set criteria -> "Save & search now" -> review the table -> "Apply with agent".

Headless / scheduled search (cron, Task Scheduler): `python -m jobfinder`
Or set `JOBS_AUTO_RUN_HOURS=24` in `.env` to re-run the saved criteria while the app is running.

## How discovery works
- Sources: company boards (Greenhouse, Lever) from `companies.json`, Adzuna (needs a free key), and two
  zero-config remote-job feeds that need no API key: **RemoteOK** and **Jobicy**. Toggle any of them under
  "Sources" in the criteria box on the Jobs page.
  - RemoteOK (remoteok.com) is a broad, mostly US/global tech-remote feed.
  - Jobicy (jobicy.com) supports `geo=usa` filtering for remote-US roles specifically; for other countries
    (e.g. India) it returns its worldwide remote feed, filtered locally like every other source.
  - For more Bangalore/India coverage, add the company boards you care about to `companies.json`: many
    Indian tech companies (fintechs, SaaS startups, etc.) post on public Greenhouse (`job-boards.greenhouse.io/<slug>`)
    or Lever (`jobs.lever.co/<slug>`) boards - the `<slug>` in that URL is what goes in `companies.json`.
  - Adzuna already covers India (`country: "India"` in criteria) - add "Bengaluru"/"Bangalore" to Locations
    for city-specific results, or leave Locations empty to search the whole country.
- Cheap rules first (title, location, work mode, age, excluded words/companies, salary). Only survivors
  reach the LLM, which extracts work mode / years required / pay / location eligibility and scores fit.
- Everything seen is stored in `jobs.db`; re-runs only process new postings. `MAX_LLM_PER_RUN` caps cost;
  leftovers wait for the next run.
- Your contact details are never sent to the screening model (only skills/experience from PROFILE).

## Request budgets (Adzuna free tier)
Adzuna free tier: 2,500 calls/month, 250/day, 25/minute. The finder enforces limits below those:
- Every Adzuna call is counted in `jobs.db` (survives restarts) and blocked at 200/day and 2,000/month
  (`ADZUNA_MAX_PER_DAY`, `ADZUNA_MAX_PER_MONTH`); calls are spaced 3 s apart (`ADZUNA_MIN_INTERVAL`).
- Adzuna runs at most once per `ADZUNA_EVERY_HOURS` (default 24); company boards run more often.
  A manual "Save & search now" bypasses these cadence guards but never the daily/monthly caps.
- A 429 or any error from Adzuna stops that run's Adzuna calls immediately; results already fetched are kept.
- Calls per run = titles x (locations + 1 remote pass), capped by `ADZUNA_MAX_QUERIES_PER_RUN` (12).
  Example: 3 titles, 2 locations = 9 calls/day = about 270/month.
- Greenhouse/Lever public boards: no published hard limits that I could find, so each board is fetched at
  most every `BOARD_MIN_HOURS` (3) and requests are spaced out (Lever 1 s).
- RemoteOK and Jobicy: also no published hard limits; each is fetched at most every `REMOTEOK_MIN_HOURS` /
  `JOBICY_MIN_HOURS` (6h default). Jobicy issues one call per title per run, capped at
  `JOBICY_MAX_QUERIES_PER_RUN` (6).
- The /jobs page shows today's and this month's Adzuna usage.

## Files
app.py, config.py, store.py, user_profile.py (your PROFILE dict)
agent/{core,browser,autopilot}.py        - the apply agent: decide/act loop, browser control, unattended runner
jobfinder/{sources,filters,rank,db,pipeline,api,settings,models}.py  - job discovery
Logs + a screenshot per apply step: `runs/<timestamp>/`.
