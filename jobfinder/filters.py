"""Cheap, free, rule-based filters. Run before (hard_filter) and after (post_filter) the LLM
scoring step, so the paid model only ever sees postings that survived the free checks."""
import re
from datetime import datetime, timezone

from .models import Criteria, Job

TOO_OLD = "too old"  # exact string: db.known_dedupe() relies on this literal to allow re-posted roles back in


def _any_word(text: str, words: list) -> bool:
    text = text.casefold()
    return any(w.casefold() in text for w in words)


def _age_days(posted_at: str) -> float | None:
    if not posted_at:
        return None
    try:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - posted).total_seconds() / 86400
    except ValueError:
        return None


def hard_filter(job: Job, c: Criteria) -> str:
    """Return a rejection reason, or "" to let the job through to LLM scoring."""
    age = _age_days(job.posted_at)
    if age is not None and age > c.max_age_days:
        return TOO_OLD

    if c.titles and not _any_word(job.title, c.titles):
        return "title doesn't match any wanted title"

    if c.exclude_keywords and _any_word(job.title, c.exclude_keywords):
        return "title has an excluded word"

    if c.exclude_companies and _any_word(job.company, c.exclude_companies):
        return "excluded company"

    if c.include_keywords and not _any_word(job.title + " " + job.description, c.include_keywords):
        return "doesn't mention any required keyword"

    return ""


def _location_ok(job_location: str, c: Criteria) -> bool:
    if not c.locations:
        return True  # no location filter: whole country is fine
    loc = job_location.casefold()
    return any(re.search(rf"\b{re.escape(l.casefold())}\b", loc) for l in c.locations) or "remote" in loc


def post_filter(job: Job, c: Criteria, score: int, location_ok: bool) -> str:
    """Run after the LLM has filled in work_mode/exp/salary and scored fit. Same contract as hard_filter."""
    if score < c.min_score:
        return f"fit score {score} below minimum {c.min_score}"

    if c.work_modes and job.work_mode and job.work_mode not in c.work_modes:
        return f"work mode '{job.work_mode}' not wanted"

    if not location_ok:
        return "location not eligible"

    if c.max_exp is not None and job.exp_min is not None and job.exp_min > c.max_exp + 0.001:
        return f"requires {job.exp_min}+ yrs, above your range"
    if c.min_exp is not None and job.exp_max is not None and job.exp_max < c.min_exp - 0.001:
        return f"caps at {job.exp_max} yrs, below your range"

    if c.min_salary_lpa is not None:
        if job.salary_max_lpa is not None and job.salary_max_lpa < c.min_salary_lpa:
            return f"salary below {c.min_salary_lpa} LPA"
        if job.salary_max_lpa is None and not c.include_unknown_salary:
            return "salary not listed"

    return ""


__all__ = ["hard_filter", "post_filter", "TOO_OLD"]
