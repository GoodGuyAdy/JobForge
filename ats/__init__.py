"""ATS (Applicant Tracking System) handler package.

Architecture
------------
Each ATS platform gets its own sub-package under ats/:

    ats/
      __init__.py          <- detect() + route entry point (this file)
      base.py              <- ATSHandler abstract base class
      workday/
        __init__.py
        handler.py         <- WorkdayHandler (deterministic + AI fallback)
        selectors.py       <- data-automation-id constants and locator helpers
        steps/
          __init__.py
          my_information.py
          my_experience.py
          application_questions.py
          voluntary_disclosures.py
          review.py

Adding a new ATS (e.g. Greenhouse, Lever) is done by:
  1. Creating ats/greenhouse/ with a handler that subclasses ATSHandler.
  2. Adding its URL detection pattern to _REGISTRY below.
  3. No other file needs to change.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Registry: maps a compiled URL-pattern to a lazy import path
# ---------------------------------------------------------------------------

# Each entry: (compiled_pattern, module_path, class_name)
# Patterns are checked in order; first match wins.
_REGISTRY: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"myworkdayjobs\.com|wd\d+\.myworkday\.com", re.IGNORECASE),
        "ats.workday.handler",
        "WorkdayHandler",
    ),
    # Future entries (uncomment to enable):
    # (re.compile(r"greenhouse\.io|boards\.greenhouse\.io", re.IGNORECASE), "ats.greenhouse.handler", "GreenhouseHandler"),
    # (re.compile(r"jobs\.lever\.co", re.IGNORECASE), "ats.lever.handler", "LeverHandler"),
    # (re.compile(r"ashbyhq\.com", re.IGNORECASE), "ats.ashby.handler", "AshbyHandler"),
    # (re.compile(r"icims\.com", re.IGNORECASE), "ats.icims.handler", "ICIMSHandler"),
    # (re.compile(r"smartrecruiters\.com", re.IGNORECASE), "ats.smartrecruiters.handler", "SmartRecruitersHandler"),
]


def detect(url: str) -> str | None:
    """Return the ATS name for *url*, or None if unrecognised.

    Detection is purely from the URL string -- no network call, no DOM needed.
    The caller should also call detect() on the *final* page URL after navigation
    so that Adzuna/aggregator redirects resolve to the real ATS host.
    """
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = url
    host = host.lower()
    for pattern, _module, _cls in _REGISTRY:
        if pattern.search(host) or pattern.search(url):
            return _module.split(".")[1]  # e.g. "workday"
    return None


def get_handler(url: str):
    """Return an instantiated ATSHandler for *url*, or None if unrecognised.

    Imports are lazy so that adding a new ATS sub-package never slows the
    startup of unrelated modules.
    """
    import importlib

    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = url
    host = host.lower()

    for pattern, module_path, class_name in _REGISTRY:
        if pattern.search(host) or pattern.search(url):
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            return cls()
    return None


__all__ = ["detect", "get_handler"]
