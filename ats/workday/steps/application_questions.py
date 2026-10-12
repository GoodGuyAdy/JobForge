"""Workday Step 3 — Application Questions.

These are employer-specific screening questions.  Because they vary by company,
this step:

  1. Reads the current page via the existing observe() infrastructure.
  2. Iterates each visible question and attempts a deterministic answer using
     PROFILE data, SAVED_ANSWERS, and DEFAULT_ANSWER_RULES.
  3. For any question it cannot answer deterministically, it falls back to the
     AI agent (returns {"fallback": True}) — the caller (handler.py) then runs
     the AI agent only for the current page and resumes deterministic handling
     on the next step.

This is the key design decision: instead of handing the ENTIRE remaining
application to the AI, we only surrender the individual page that contains
the unknown question.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from store import save_answer, saved_answers

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule-based answerers for common question patterns
# ---------------------------------------------------------------------------

_YES_NO_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Work authorisation
    (re.compile(r"legally authorized.+work|eligible to work|right to work", re.I), "Yes"),
    (re.compile(r"require.+visa|sponsorship|work authorization|work authoriz", re.I), "No"),
    # Relocation / remote
    (re.compile(r"willing to relocate", re.I), "Yes"),
    # Non-compete / NDA
    (re.compile(r"non.compete|restrictive covenant|confidentiality agreement", re.I), "No"),
    # Former employee
    (re.compile(r"previous.+employee|former.+employee|previously worked for", re.I), "No"),
    # Background check consent
    (re.compile(r"consent.+background|agree.+background check", re.I), "Yes"),
    # Newsletter / marketing opt-in
    (re.compile(r"newsletter|marketing.+email|talent.+community|sms.+update", re.I), "No"),
]

_TEXT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"how did you hear|how did you find|where did you hear", re.I), "LinkedIn"),
    (re.compile(r"notice period|when.+available|earliest.+start", re.I), "Immediately"),
]


def _answer_from_saved(question: str) -> str | None:
    """Look up a saved answer for this question."""
    norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", question.casefold())).strip()
    for entry in saved_answers():
        stored = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", entry.get("question", "").casefold())).strip()
        if stored and (stored == norm or norm in stored or stored in norm):
            return entry.get("answer", "")
    return None


def _rule_based_answer(label: str, question_type: str) -> str | None:
    """Return a rule-based answer or None if no rule applies."""
    for pattern, answer in _YES_NO_PATTERNS:
        if pattern.search(label):
            return answer
    for pattern, answer in _TEXT_PATTERNS:
        if pattern.search(label):
            return answer
    return None


# ---------------------------------------------------------------------------
# Action helpers (mirror what the AI agent does via run_action)
# ---------------------------------------------------------------------------

def _apply_answer(sess, element: dict, answer: str) -> str:
    """Apply *answer* to *element* using the appropriate action."""
    from agent.browser import run_action

    tag = element.get("tag", "")
    kind = element.get("type", "") or element.get("role", "")

    if tag == "select" or kind in ("combobox",):
        return run_action(sess, "select_option", {"id": element["id"], "option": answer})
    elif kind in ("radio", "checkbox", "switch"):
        # Radio: click the option whose label matches *answer*
        return run_action(sess, "click", {"id": element["id"]})
    else:
        return run_action(sess, "fill", {"id": element["id"], "text": answer})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fill(sess, hooks: "Hooks", profile: dict) -> dict:
    """Attempt deterministic answers for all application questions.

    Returns:
        {"ok": True}                  — all questions answered deterministically
        {"fallback": True}            — at least one question needs AI; caller
                                        should run the AI agent on this page
        {"needs_input": True, ...}    — user must answer (unattended mode)
    """
    from agent.browser import observe, current_page

    page = current_page(sess)
    obs = observe(sess)

    unanswered: list[dict] = []
    answered_count = 0

    for element in obs["elements"]:
        # Skip non-interactive and already-answered elements
        if element.get("disabled") or element.get("tag") == "button":
            continue
        if element.get("tag") == "a":
            continue
        # Check if this looks like a question field
        label = element.get("label") or element.get("group") or ""
        if not label:
            continue
        # Skip already-filled elements
        if element.get("value") and element.get("value").strip():
            log.debug("  AppQ: skip already-filled %r", label)
            answered_count += 1
            continue
        if element.get("checked"):
            log.debug("  AppQ: skip already-checked %r", label)
            answered_count += 1
            continue

        # Try saved answers first
        answer = _answer_from_saved(label)

        # Then rule-based
        if answer is None:
            answer = _rule_based_answer(label, element.get("type", ""))

        if answer is not None:
            log.info("  AppQ: answering %r → %r", label[:60], answer)
            result = _apply_answer(sess, element, answer)
            log.info("  AppQ result: %s", result)
            answered_count += 1
        else:
            log.info("  AppQ: no deterministic answer for %r — flagging for fallback", label[:60])
            unanswered.append(element)

    if unanswered:
        log.info("  AppQ: %d unanswered questions → falling back to AI for this page", len(unanswered))
        return {"fallback": True, "unanswered": [e.get("label") or e.get("group") for e in unanswered]}

    log.info("  AppQ: all %d questions answered deterministically", answered_count)
    return {"ok": True}
