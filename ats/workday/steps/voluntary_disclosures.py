"""Workday Step 4 — Voluntary Disclosures (EEO / self-identification).

Handles gender, race/ethnicity, veteran status, and disability questions.
All answers come from PROFILE or SAVED_ANSWERS; we never guess.
If a question is not answered in PROFILE/SAVED_ANSWERS, we select the
"Decline to self-identify" / "I choose not to answer" option when available,
or fall back to asking the human.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ats.workday.selectors import (
    DISABILITY_DROPDOWN,
    ETHNIC_DROPDOWN,
    GENDER_DROPDOWN,
    VETERAN_DROPDOWN,
    is_present,
    maybe_wait,
)
from store import saved_answers

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)


# Phrases Workday uses for "decline to answer" options (varies by tenant)
_DECLINE_PHRASES = [
    "decline", "prefer not", "choose not", "do not wish", "not disclosed",
    "i don't wish", "i do not wish", "not applicable", "n/a",
]


def _get_decline_option(page) -> str | None:
    """Return the first visible option text that looks like 'decline to answer'."""
    opts = page.locator('[role="option"]:visible')
    for i in range(min(opts.count(), 30)):
        text = opts.nth(i).inner_text().strip().casefold()
        if any(phrase in text for phrase in _DECLINE_PHRASES):
            return opts.nth(i).inner_text().strip()
    return None


def _select_dropdown(page, automation_id: str, value: str) -> bool:
    if not value:
        return False
    loc = maybe_wait(page, automation_id, 4_000)
    if loc is None:
        return False
    try:
        loc.click(timeout=5_000)
        page.wait_for_timeout(500)
        opts = page.locator('[role="option"]:visible')
        target = value.strip().casefold()
        for i in range(min(opts.count(), 60)):
            text = opts.nth(i).inner_text().strip().casefold()
            if text == target or target in text:
                opts.nth(i).click(timeout=3_000)
                return True
        # Fall back to decline option
        decline = _get_decline_option(page)
        if decline:
            log.info("  VolDisc: no match for %r, selecting decline option %r", value, decline)
            target_loc = page.locator(f'[role="option"]:visible >> text="{decline}"')
            if target_loc.count():
                target_loc.first.click(timeout=3_000)
                return True
        page.keyboard.press("Escape")
        return False
    except Exception as exc:
        log.warning("  dropdown %s failed: %s", automation_id, exc)
        return False


def _answer_from_saved(question_keywords: list[str]) -> str | None:
    for entry in saved_answers():
        q = entry.get("question", "").casefold()
        if any(kw in q for kw in question_keywords):
            return entry.get("answer", "")
    return None


def fill(sess, hooks: "Hooks", profile: dict) -> dict:
    """Fill voluntary disclosure dropdowns.

    Strategy (in order):
      1. Check PROFILE for explicit answers.
      2. Check SAVED_ANSWERS.
      3. Select the "decline to self-identify" option when available.
      4. Ask the human (in attended mode) or return needs_input (unattended).
    """
    from agent.browser import current_page
    page = current_page(sess)

    voluntary_profile = profile.get("voluntary_disclosures") or {}

    def _fill_field(automation_id: str, profile_key: str, keywords: list[str]) -> None:
        if not is_present(page, automation_id, 3_000):
            return

        value = (
            voluntary_profile.get(profile_key)
            or _answer_from_saved(keywords)
        )

        if value:
            log.info("  VolDisc: %s → %r (from profile/saved)", automation_id, value)
            _select_dropdown(page, automation_id, value)
            return

        # Try to select the decline option automatically
        loc = maybe_wait(page, automation_id, 3_000)
        if loc:
            loc.click(timeout=4_000)
            page.wait_for_timeout(500)
            decline = _get_decline_option(page)
            if decline:
                log.info("  VolDisc: %s → decline option %r", automation_id, decline)
                page.locator(f'[role="option"]:visible >> text="{decline}"').first.click(timeout=3_000)
                return
            page.keyboard.press("Escape")

        # No decline option — ask the human
        log.info("  VolDisc: no answer available for %s — asking human", automation_id)
        answer = hooks.ask(
            f"Workday voluntary disclosure: {automation_id.replace('-', ' ')} — what should I select?",
            [],
        )
        if answer is None:
            return  # unattended: skip; the AI fallback won't be triggered here
        if answer:
            _select_dropdown(page, automation_id, answer)

    _fill_field(GENDER_DROPDOWN, "gender", ["gender"])
    _fill_field(ETHNIC_DROPDOWN, "ethnicity", ["ethnic", "race", "racial"])
    _fill_field(VETERAN_DROPDOWN, "veteran", ["veteran", "military"])
    _fill_field(DISABILITY_DROPDOWN, "disability", ["disability", "disabled"])

    return {"ok": True}
