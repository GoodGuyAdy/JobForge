"""Workday Step 5 — Review.

The Review step just reads the final summary page. We do NOT submit here
(submission is handled by the existing autopilot/core layer after human
approval or auto-gate checks).  We simply:
  1. Confirm we are on the Review step.
  2. Take a final screenshot.
  3. Return ready_for_review so the existing flow can take over.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ats.workday.selectors import REVIEW_SECTION, is_present

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)


def fill(sess, hooks: "Hooks", profile: dict) -> dict:
    """Confirm the Review page is visible and surface it for human approval."""
    from agent.browser import current_page, blank_required, observe

    page = current_page(sess)

    on_review = is_present(page, REVIEW_SECTION, 5_000)
    if not on_review:
        log.info("  Review: review section not detected — may still be loading")
        page.wait_for_timeout(2_000)
        on_review = is_present(page, REVIEW_SECTION, 5_000)

    obs = observe(sess)
    blanks = blank_required(list(sess.elements.values()))

    log.info("  Review: review page detected=%s, blank_required=%s", on_review, blanks)
    return {
        "ok": True,
        "blank_required": blanks,
        "review_page_detected": on_review,
    }
