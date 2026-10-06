"""Fetches postings from company boards (Greenhouse, Lever) and Adzuna. See README for the request
budgets this enforces. Nothing here scores or filters jobs; that's filters.py / rank.py."""
import html
import re
import time
from contextlib import contextmanager

import httpx

import config

from . import db
from .models import Criteria, Job

_ADZUNA_COUNTRY = {
    "india": "in", "united states": "us", "usa": "us", "united kingdom": "gb", "uk": "gb",
    "canada": "ca", "australia": "au", "germany": "de", "france": "fr", "netherlands": "nl",
    "singapore": "sg", "new zealand": "nz", "poland": "pl", "brazil": "br", "austria": "at",
    "south africa": "za", "italy": "it",
}


@contextmanager
def client():
    with httpx.Client(timeout=20, headers={"User-Agent": "AutoApplyAI-JobFinder/1.0"}) as http:
        yield http


def _wait_before_request(delay: float, should_continue) -> bool:
    if not should_continue():
        return False
    if delay:
        time.sleep(delay)
    return should_continue()


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _get(http: httpx.Client, url: str, log, label: str, **kwargs):
    try:
        r = http.get(url, **kwargs)
        r.raise_for_status()
        return r
    except httpx.HTTPError as e:
        log(f"{label}: {e}")
        return None


# ----------------------------------------------------------------------------
# Company boards: no published hard rate limits, but we stay polite (config.BOARD_MIN_HOURS).
# ----------------------------------------------------------------------------

def _fetch_greenhouse(http: httpx.Client, slug: str, log) -> list:
    key = f"board:greenhouse:{slug}"
    if (db.hours_since(key) or 999) < config.BOARD_MIN_HOURS:
        return []
    r = _get(http, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", log, f"greenhouse/{slug}",
              params={"content": "true"})
    db.mark_run(key)
    if r is None:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append(Job(
            source="greenhouse", external_id=str(j.get("id", "")), url=j.get("absolute_url", ""),
            company=slug, title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
            description=_strip_html(j.get("content", ""))[:8000], posted_at=(j.get("updated_at") or "")[:10]))
    return out


def _fetch_lever(http: httpx.Client, slug: str, log, should_continue) -> list:
    key = f"board:lever:{slug}"
    if (db.hours_since(key) or 999) < config.BOARD_MIN_HOURS:
        return []
    if not _wait_before_request(config.LEVER_MIN_INTERVAL, should_continue):
        return []
    r = _get(http, f"https://api.lever.co/v0/postings/{slug}", log, f"lever/{slug}", params={"mode": "json"})
    db.mark_run(key)
    if r is None:
        return []
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        posted = j.get("createdAt")
        out.append(Job(
            source="lever", external_id=str(j.get("id", "")), url=j.get("hostedUrl", ""),
            company=slug, title=j.get("text", ""), location=cats.get("location", ""),
            description=_strip_html(j.get("descriptionPlain") or j.get("description", ""))[:8000],
            posted_at=time.strftime("%Y-%m-%d", time.gmtime(posted / 1000)) if posted else ""))
    return out


def _fetch_boards(http: httpx.Client, companies: list, log, should_continue) -> list:
    found = []
    for co in companies:
        if not should_continue():
            break
        board, slug = (co.get("board") or "").casefold(), co.get("slug", "")
        if not slug:
            continue
        try:
            if board == "greenhouse":
                found += _fetch_greenhouse(http, slug, log)
            elif board == "lever":
                found += _fetch_lever(http, slug, log, should_continue)
        except Exception as e:
            log(f"{board}/{slug}: {type(e).__name__}: {e}")
    return found


# ----------------------------------------------------------------------------
# RemoteOK and Jobicy: free global remote-job feeds, no API key needed. Unlike company boards and
# Adzuna these aren't India-specific, but they're a strong source of remote roles open to India/anywhere,
# and Jobicy's `geo=usa` filter targets remote-US postings specifically.
# ----------------------------------------------------------------------------

def _fetch_remoteok(http: httpx.Client, log) -> list:
    key = "board:remoteok"
    if (db.hours_since(key) or 999) < config.REMOTEOK_MIN_HOURS:
        return []
    r = _get(http, "https://remoteok.com/api", log, "remoteok")
    db.mark_run(key)
    if r is None:
        return []
    out = []
    for j in r.json():
        if not j.get("id") or not j.get("url"):
            continue  # the first element is an API-terms notice, not a job
        out.append(Job(
            source="remoteok", external_id=str(j["id"]), url=j.get("url", ""),
            company=j.get("company", ""), title=j.get("position", ""),
            location=j.get("location") or "Remote",
            description=_strip_html(j.get("description", ""))[:8000], posted_at=(j.get("date") or "")[:10]))
    return out


def _fetch_jobicy_title(http: httpx.Client, title: str, geo: str | None, log) -> list:
    params = {"count": 50, "tag": title}
    if geo:
        params["geo"] = geo
    r = _get(http, "https://jobicy.com/api/v2/remote-jobs", log, "jobicy", params=params)
    if r is None:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append(Job(
            source="jobicy", external_id=str(j.get("id", "")), url=j.get("url", ""),
            company=j.get("companyName", ""), title=j.get("jobTitle", ""),
            location=j.get("jobGeo") or "Remote",
            description=_strip_html(j.get("jobDescription", ""))[:8000], posted_at=(j.get("pubDate") or "")[:10]))
    return out


def _fetch_jobicy(http: httpx.Client, c: Criteria, log, should_continue) -> list:
    key = "board:jobicy"
    if (db.hours_since(key) or 999) < config.JOBICY_MIN_HOURS:
        return []
    if not c.titles:
        return []
    geo = "usa" if c.country.strip().casefold() in ("united states", "usa", "us") else None
    out = []
    requested = False
    for title in c.titles[:config.JOBICY_MAX_QUERIES_PER_RUN]:
        if not should_continue():
            break
        requested = True
        out.extend(_fetch_jobicy_title(http, title, geo, log))
    if requested:
        db.mark_run(key)
    return out


# ----------------------------------------------------------------------------
# Adzuna: free-tier limits enforced here (see README "Request budgets").
# ----------------------------------------------------------------------------

def _adzuna_query(http: httpx.Client, c: Criteria, title: str, where: str, log) -> list:
    if db.calls_today("adzuna") >= config.ADZUNA_MAX_PER_DAY or \
            db.calls_this_month("adzuna") >= config.ADZUNA_MAX_PER_MONTH:
        log("adzuna: daily/monthly call budget reached, skipping remaining queries")
        return None  # sentinel: caller stops issuing further Adzuna calls this run
    country = _ADZUNA_COUNTRY.get(c.country.strip().casefold(), "in")
    params = {
        "app_id": config.ADZUNA_APP_ID, "app_key": config.ADZUNA_APP_KEY, "what": title,
        "max_days_old": c.max_age_days, "results_per_page": 50, "content-type": "application/json",
    }
    if where:
        params["where"] = where
    r = _get(http, f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", log, "adzuna", params=params)
    db.record_call("adzuna")
    if r is None:
        return None  # treat any error (incl. 429) as "stop calling Adzuna this run"
    out = []
    for j in r.json().get("results", []):
        loc = (j.get("location") or {}).get("display_name", "")
        out.append(Job(
            source="adzuna", external_id=str(j.get("id", "")), url=j.get("redirect_url", ""),
            company=(j.get("company") or {}).get("display_name", ""), title=j.get("title", ""),
            location=loc, description=_strip_html(j.get("description", ""))[:8000],
            posted_at=(j.get("created") or "")[:10],
            salary_min_lpa=(j["salary_min"] / 100000) if j.get("salary_min") else None,
            salary_max_lpa=(j["salary_max"] / 100000) if j.get("salary_max") else None))
    return out


def _fetch_adzuna(http: httpx.Client, c: Criteria, log, force: bool, should_continue) -> list:
    if not (config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY):
        return []
    if not force and (db.hours_since("adzuna") or 999) < config.ADZUNA_EVERY_HOURS:
        return []
    if not c.titles:
        return []
    locations = c.locations or [""]  # "" = whole country; always add one remote-wide pass
    queries = [(t, w) for t in c.titles for w in [*locations, ""]]
    queries = queries[:config.ADZUNA_MAX_QUERIES_PER_RUN]

    found = []
    requested = False
    for i, (title, where) in enumerate(queries):
        delay = config.ADZUNA_MIN_INTERVAL if i else 0
        if not _wait_before_request(delay, should_continue):
            break
        requested = True
        jobs = _adzuna_query(http, c, title, where, log)
        if jobs is None:
            break  # budget hit or an error: stop calling Adzuna for the rest of this run
        found += jobs
    if requested:
        db.mark_run("adzuna")
    return found


def fetch_all(http: httpx.Client, c: Criteria, companies: list, log, force: bool = False,
              should_continue=None) -> list:
    should_continue = should_continue or (lambda: True)
    found = []
    if "companies" in c.sources and should_continue():
        found += _fetch_boards(http, companies, log, should_continue)
    if "adzuna" in c.sources and should_continue():
        found += _fetch_adzuna(http, c, log, force, should_continue)
    if "remoteok" in c.sources and should_continue():
        found += _fetch_remoteok(http, log)
    if "jobicy" in c.sources and should_continue():
        found += _fetch_jobicy(http, c, log, should_continue)
    return found
