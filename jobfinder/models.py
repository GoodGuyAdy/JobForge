"""Shared data shapes for the job finder: the raw posting (Job) and the user's search (Criteria)."""
import hashlib
import re
from typing import Optional

from pydantic import BaseModel, Field


class Job(BaseModel):
    source: str  # "greenhouse" | "lever" | "adzuna"
    external_id: str = ""
    url: str
    company: str
    title: str
    location: str = ""
    description: str = ""
    posted_at: str = ""  # ISO date string, "" if the source doesn't say
    # The next five are unknown at fetch time; rank.py fills them in from the LLM.
    work_mode: Optional[str] = None  # "remote" | "hybrid" | "onsite"
    exp_min: Optional[float] = None
    exp_max: Optional[float] = None
    salary_min_lpa: Optional[float] = None
    salary_max_lpa: Optional[float] = None


class Criteria(BaseModel):
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    country: str = "India"
    work_modes: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    min_exp: Optional[float] = None
    max_exp: Optional[float] = None
    min_salary_lpa: Optional[float] = None
    include_unknown_salary: bool = True
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    max_age_days: int = 14
    min_score: int = 0
    sources: list[str] = Field(default_factory=lambda: ["companies", "adzuna", "remoteok", "jobicy"])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.casefold())).strip()


def job_key(job: Job) -> str:
    """Identifies one specific posting: same source + external id (or url if a source has no id)."""
    return f"{job.source}:{job.external_id or job.url}"


def dedupe_key(job: Job) -> str:
    """Identifies the same role even if re-posted, or listed on more than one board."""
    return hashlib.sha1(f"{_norm(job.company)}|{_norm(job.title)}".encode()).hexdigest()
