"""Central settings. Edit DEFAULT_ANSWER_RULES to change how routine questions are answered."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# `profile` collides with a stdlib module name, so prefer user_profile.py if you rename it.
try:
    from user_profile import PROFILE
except ImportError:
    try:
        from user_profile import USER_PROFILE as PROFILE
    except ImportError:
        from profile import PROFILE

ROOT = Path(__file__).parent

MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
MODEL_STRONG = os.getenv(
    "OPENAI_MODEL_STRONG", MODEL
)  # used automatically when the agent gets stuck
JOB_PORTAL_ACCOUNT_PASSWORD = os.getenv("JOB_PORTAL_ACCOUNT_PASSWORD", "")
MAX_STEPS = int(os.getenv("MAX_STEPS", "80"))
MAX_ELEMENTS = 200  # cap on interactive elements sent to the model per step

VIEWPORT = {"width": 1400, "height": 900}
USER_DATA_DIR = ROOT / "browser_profile"  # persistent logins/cookies live here
RUNS_DIR = ROOT / "runs"  # per-run logs + screenshots
QA_BANK_PATH = ROOT / "qa_bank.json"  # answers you gave via ask_human, reused next time

_DOC_CANDIDATES = {
    "resume": os.getenv("RESUME_PATH", str(ROOT / "docs" / "resume.pdf")),
    "cover_letter": os.getenv(
        "COVER_LETTER_PATH", str(ROOT / "docs" / "cover_letter.pdf")
    ),
}


def available_docs() -> dict:
    """Only these files can ever be uploaded. The model names a doc; it never supplies a path."""
    return {
        name: str(Path(p).resolve())
        for name, p in _DOC_CANDIDATES.items()
        if Path(p).is_file()
    }


# Carried over from the original app's prompt. Edit freely.
DEFAULT_ANSWER_RULES = """\
- "How did you hear about us?" with no better answer available: use "LinkedIn".
- Newsletters, marketing email, SMS/WhatsApp updates, talent-community opt-ins: answer No / leave unchecked.
- "Have you ever been issued a <company> employee ID or corporate email?" : answer No unless PROFILE
  explicitly says otherwise. Do not infer Yes from current or past employers.
- Questions asking whether you are able to work in the stated region, location, country, or time zone:
  answer Yes when the answer is consistent with PROFILE. Do not infer availability beyond what PROFILE states.
- Questions asking whether you have signed a non-compete, restrictive covenant, confidentiality agreement,
  or similar agreement with your current or previous employer: answer No unless PROFILE explicitly says otherwise.
- Do not answer other legal declarations, work authorization/sponsorship, or voluntary self-identification
  (gender, race, veteran, disability) from guesses. Use PROFILE/SAVED_ANSWERS or ask_human.
"""


# ----------------------------------------------------------------------------
# Job discovery (jobfinder/)
# ----------------------------------------------------------------------------
JOBS_DB_PATH = ROOT / "jobs.db"
CRITERIA_PATH = ROOT / "criteria.json"  # saved from the Jobs page
COMPANIES_PATH = (
    ROOT / "companies.json"
)  # target companies (see companies.example.json)
JOBS_MODEL = os.getenv(
    "JOBS_MODEL", "gpt-6-luna"
)  # screening model; a cheaper one is fine
MAX_LLM_PER_RUN = int(
    os.getenv("MAX_LLM_PER_RUN", "60")
)  # cost cap; leftovers stay 'pending' for the next run
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # optional new-match alerts
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Request budgets (job sources) ---
# Adzuna free tier: 2,500 calls/month, 250/day, 25/minute. Defaults keep a safety margin under each.
ADZUNA_MAX_PER_DAY = int(os.getenv("ADZUNA_MAX_PER_DAY", "200"))
ADZUNA_MAX_PER_MONTH = int(os.getenv("ADZUNA_MAX_PER_MONTH", "2000"))
ADZUNA_MIN_INTERVAL = float(
    os.getenv("ADZUNA_MIN_INTERVAL", "3")
)  # seconds between calls (=> max 20/min)
ADZUNA_MAX_QUERIES_PER_RUN = int(os.getenv("ADZUNA_MAX_QUERIES_PER_RUN", "12"))
ADZUNA_EVERY_HOURS = float(
    os.getenv("ADZUNA_EVERY_HOURS", "24")
)  # Adzuna runs at most this often (unless forced)
# Greenhouse/Lever public boards publish no hard limits (as far as I could find): stay polite.
BOARD_MIN_HOURS = float(
    os.getenv("BOARD_MIN_HOURS", "3")
)  # don't refetch the same board more often
LEVER_MIN_INTERVAL = float(
    os.getenv("LEVER_MIN_INTERVAL", "1")
)  # Lever's robots.txt asks for a 1s crawl delay
# RemoteOK (remoteok.com/api) and Jobicy (jobicy.com/api/v2/remote-jobs): free, global remote-job feeds,
# no API key needed. No published rate limits either, so re-fetched at most every N hours out of politeness.
REMOTEOK_MIN_HOURS = float(os.getenv("REMOTEOK_MIN_HOURS", "6"))
JOBICY_MIN_HOURS = float(os.getenv("JOBICY_MIN_HOURS", "6"))
JOBICY_MAX_QUERIES_PER_RUN = int(
    os.getenv("JOBICY_MAX_QUERIES_PER_RUN", "6")
)  # one call per title, capped


# ----------------------------------------------------------------------------
# Autopilot (finder -> apply agent, unattended). See autopilot.py
# ----------------------------------------------------------------------------
# off    = nothing automatic.
# review = the agent fills each queued application and PARKS it (tab left open) for your approval.
# auto   = the agent also submits, but only when strict gates pass; anything doubtful is parked.
# Both of these can also be changed at runtime from the Jobs page (no restart needed); that choice is
# persisted to automation.json and wins over the .env value on the next restart too.
AUTOMATION_PATH = ROOT / "automation.json"


def _load_automation() -> dict:
    try:
        return json.loads(AUTOMATION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


_automation = _load_automation()
AUTOPILOT_MODE = _automation.get(
    "mode", os.getenv("AUTOPILOT_MODE", "off").strip().lower()
)
JOBS_AUTO_RUN_HOURS = float(
    _automation.get("auto_run_hours", os.getenv("JOBS_AUTO_RUN_HOURS", "0"))
)


def set_autopilot_mode(mode: str) -> None:
    global AUTOPILOT_MODE
    AUTOPILOT_MODE = mode
    _save_automation()


def set_auto_run_hours(hours: float) -> None:
    global JOBS_AUTO_RUN_HOURS
    JOBS_AUTO_RUN_HOURS = hours
    _save_automation()


def _save_automation() -> None:
    AUTOMATION_PATH.write_text(
        json.dumps({"mode": AUTOPILOT_MODE, "auto_run_hours": JOBS_AUTO_RUN_HOURS}),
        encoding="utf-8",
    )


AUTO_QUEUE_MIN_SCORE = int(
    os.getenv("AUTO_QUEUE_MIN_SCORE", "75")
)  # fit score needed to be queued automatically
AUTO_SUBMIT_MIN_SCORE = int(
    os.getenv("AUTO_SUBMIT_MIN_SCORE", "85")
)  # fit score needed to auto-SUBMIT
# Only company-board jobs are queued automatically: aggregator redirects lead to arbitrary sites.
AUTO_SOURCES = [
    s.strip()
    for s in os.getenv("AUTO_SOURCES", "greenhouse,lever").split(",")
    if s.strip()
]
AUTO_MAX_ATTEMPTS_PER_DAY = int(os.getenv("AUTO_MAX_ATTEMPTS_PER_DAY", "15"))
AUTO_MAX_SUBMITS_PER_DAY = int(os.getenv("AUTO_MAX_SUBMITS_PER_DAY", "10"))
AUTO_MAX_ATTEMPTS_PER_JOB = int(os.getenv("AUTO_MAX_ATTEMPTS_PER_JOB", "2"))
AUTO_COOLDOWN_SECONDS = float(
    os.getenv("AUTO_COOLDOWN_SECONDS", "120")
)  # pause between applications (randomised)
AUTO_MAX_PARKED = int(
    os.getenv("AUTO_MAX_PARKED", "5")
)  # filled forms waiting for you (tabs stay open)
AUTO_FAILURE_PAUSE = int(
    os.getenv("AUTO_FAILURE_PAUSE", "5")
)  # consecutive failures before auto-pausing
HEADLESS = (
    os.getenv("HEADLESS", "0") == "1"
)  # run the browser without a window (some sites block this)
