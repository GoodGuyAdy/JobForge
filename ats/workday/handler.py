"""WorkdayHandler: orchestrates the full Workday application flow deterministically.

Design principles
-----------------
1.  DETERMINISTIC FIRST  — every known Workday interaction is performed directly
    via Playwright without AI:
    - Landing on job posting & clicking "Apply"
    - Selecting "Autofill with Resume" (or "Apply Manually")
    - Prompting human for Sign-In / Account creation and waiting for completion
    - Uploading resume on "Autofill with Resume" page and clicking Continue
    - Step 1: My Information
    - Step 2: My Experience
    - Step 3: Application Questions (rule-based & saved answers)
    - Step 4: Voluntary Disclosures
    - Step 5: Review

2.  FIELD-LEVEL AI FALLBACK — only when a step contains unknown questions or custom
    fields that deterministic rules cannot handle (e.g. unknown employer screening
    questions in Step 3), the AI agent is called for THAT PAGE ONLY.  After the AI
    answers the fields, control immediately returns to the deterministic handler to
    advance to the next step.

3.  FULL AI FALLBACK  — as an absolute last resort if an unexpected error cannot be
    recovered from.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import config
from ats.base import ATSHandler
from ats.workday.selectors import (
    APPLY_BUTTON,
    APPLY_MANUALLY_BUTTON,
    APPLY_WITH_RESUME_BUTTON,
    AUTOFILL_RESUME_CONTINUE,
    CREATE_ACCOUNT_BUTTON,
    LOADING_SPINNER,
    NEXT_BUTTON,
    REVIEW_SECTION,
    SAVE_CONTINUE_BUTTON,
    SIGN_IN_BUTTON,
    SIGN_IN_SUBMIT_BUTTON,
    STEP_INDICATOR_CONTAINER,
    STEP_NAMES,
    SUBMIT_BUTTON,
    current_step_name,
    detect_step_from_url,
    is_present,
    is_sign_in_page,
    maybe_wait,
    sel,
    wait_for,
)
from store import log_step

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)

_MAX_STEP_RETRIES = 2
_SETTLE_MS = 1_500


def _settle(page, ms: int = _SETTLE_MS) -> None:
    """Wait for Workday's JS to finish rendering after a navigation action."""
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def _click_next(page) -> bool:
    """Click the 'Next', 'Save and Continue', or 'Continue' button. Returns True on success."""
    for aid in (NEXT_BUTTON, SAVE_CONTINUE_BUTTON, AUTOFILL_RESUME_CONTINUE):
        loc = maybe_wait(page, aid, 3_000)
        if loc and loc.is_visible() and loc.is_enabled():
            loc.click(timeout=8_000)
            return True
    # Fallback: look for visible button by text
    candidates = [
        'button:has-text("Save and Continue")',
        'button:has-text("Save & Continue")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
    ]
    for sel_str in candidates:
        btn = page.locator(sel_str).first
        if btn.count() and btn.is_visible() and btn.is_enabled():
            btn.click(timeout=8_000)
            return True
    return False


def _detect_current_step(page) -> str:
    """Return the name of the current Workday step or phase (best-effort)."""
    # 1. Review step
    if is_present(page, REVIEW_SECTION, 1_000):
        return "Review"
    submit_btn = page.locator(f'{sel(SUBMIT_BUTTON)}, button:has-text("Submit")')
    if submit_btn.count() > 0 and submit_btn.first.is_visible():
        if current_step_name(page) == "Review" or is_present(page, REVIEW_SECTION, 500):
            return "Review"

    # 2. Progress breadcrumbs / active step
    name = current_step_name(page)
    if name:
        return name

    # 3. Known section containers
    heuristics = [
        (REVIEW_SECTION, "Review"),
        ("voluntaryDisclosuresSection", "Voluntary Disclosures"),
        ("applicationQuestions", "Application Questions"),
        ("workExperienceSection", "My Experience"),
        ("legalName", "My Information"),
    ]
    for automation_id, step in heuristics:
        if is_present(page, automation_id, 1_000):
            return step

    # 4. Sign In / Create Account wall
    if is_sign_in_page(page):
        return "Sign In"

    # 5. "Autofill with Resume" upload page
    file_inputs = page.locator('input[type="file"]')
    if file_inputs.count() > 0:
        page_text = ""
        try:
            page_text = page.locator("body").inner_text()[:1000].casefold()
        except Exception:
            pass
        if (
            "autofill with resume" in page_text
            or "upload resume" in page_text
            or is_present(page, "drop-zone", 500)
            or page.locator('button:has-text("Continue"), [data-automation-id="bottom-navigation-next-btn"]').count() > 0
        ):
            # Only treat as Autofill with Resume if not already on My Experience or My Information
            if not is_present(page, "workExperienceSection", 500) and not is_present(page, "legalName", 500):
                return "Autofill with Resume"

    # 6. Apply Options modal / page (Autofill with Resume / Apply Manually choices)
    apply_with_resume = page.locator(
        f'{sel(APPLY_WITH_RESUME_BUTTON)}, '
        'button:has-text("Autofill with Resume"), '
        'a:has-text("Autofill with Resume")'
    )
    apply_manually = page.locator(
        f'{sel(APPLY_MANUALLY_BUTTON)}, '
        'button:has-text("Apply Manually"), '
        'a:has-text("Apply Manually")'
    )
    if (apply_with_resume.count() > 0 and apply_with_resume.first.is_visible()) or (
        apply_manually.count() > 0 and apply_manually.first.is_visible()
    ):
        return "Apply Options"

    # 7. Job Posting page with "Apply" button
    apply_btn = page.locator(
        f'{sel(APPLY_BUTTON)}, '
        f'{sel("job-posting-apply-button")}, '
        f'{sel("adventureButton")}, '
        'button:has-text("Apply"), '
        'a:has-text("Apply"), '
        '[aria-label="Apply"]'
    ).first
    if apply_btn.count() > 0 and apply_btn.is_visible():
        return "Job Posting"

    # 8. URL heuristic
    url_step = detect_step_from_url(page.url)
    if url_step:
        return url_step

    return "Unknown"


def _run_ai_for_page(sess, task: str, hooks: "Hooks", run_dir, step_label: str) -> dict:
    """Run the AI agent for the CURRENT page only.

    The AI's job is solely to answer/fill the questions on this page.
    Once filled, it calls finish(status='ready_for_review', summary='Step fields completed').
    The deterministic handler will then advance to the next step.
    """
    from agent.core import run_agent

    ai_task = (
        f"You are assisting with the Workday step '{step_label}'. "
        "Your ONLY goal is to fill or answer any unanswered questions or required fields visible on THIS page "
        "using PROFILE and SAVED_ANSWERS. "
        "Once all visible questions on this page are answered/selected, call finish(status='ready_for_review', summary='Page questions answered'). "
        "Do not click Next, Save and Continue, or Submit — the system handles navigation between steps."
    )
    log.info("  WorkdayHandler: running AI agent for page '%s'", step_label)
    result = run_agent(sess, ai_task, hooks, run_dir, max_steps=20)
    log.info("  AI agent result for page '%s': %s", step_label, result.get("status"))
    return result


# ---------------------------------------------------------------------------
# WorkdayHandler
# ---------------------------------------------------------------------------

class WorkdayHandler(ATSHandler):
    """Deterministic Workday application handler with AI fallback."""

    name = "Workday"

    def run(self, sess: "Session", hooks: "Hooks", run_dir, task: str) -> dict:
        from agent.browser import current_page, snapshot
        from ats.workday.steps import my_information
        from ats.workday.steps import my_experience
        from ats.workday.steps import application_questions
        from ats.workday.steps import voluntary_disclosures
        from ats.workday.steps import review as review_step

        page = current_page(sess)
        profile = config.PROFILE
        step_count = 0
        review_notes: list[str] = []

        hooks.emit("WorkdayHandler: starting deterministic Workday flow")

        max_iterations = 30  # covers setup phases + multi-step form

        for iteration in range(max_iterations):
            if hooks.stopped():
                return {"status": "stopped", "summary": "Stopped by user.", "steps": step_count}

            page = current_page(sess)
            _settle(page, 800)
            current_step = _detect_current_step(page)
            hooks.emit(f"WorkdayHandler: step '{current_step}' (iteration {iteration + 1})")
            log.info("WorkdayHandler iteration %d: step=%r", iteration, current_step)

            try:
                snapshot(sess, path=run_dir / f"wd_step_{iteration:02d}.jpg")
            except Exception:
                pass

            # ── Step: Sign In / Account wall (whenever sign in appears) ──
            if is_sign_in_page(page) or current_step == "Sign In":
                current_step = "Sign In"
                hooks.emit(f"WorkdayHandler: step 'Sign In' (iteration {iteration + 1})")
                hooks.emit("WorkdayHandler: Workday Sign In page detected. Waiting for user to sign in...")
                answer = hooks.ask(
                    "This application is at the Workday Sign In page. "
                    "Please sign in or create your account in the browser, "
                    "then let me know when it is done so I can continue.",
                    ["Done", "I've signed in"],
                )
                if hooks.stopped():
                    return {"status": "stopped", "summary": "Stopped by user.", "steps": step_count}
                if answer is None:
                    return {
                        "status": "needs_input",
                        "summary": "Please sign in or create a Workday account in the browser.",
                        "review_notes": review_notes,
                        "blank_required": [],
                        "question": "Please sign in or create a Workday account in the browser, then let me know when it is done.",
                        "choices": ["Done", "I've signed in"],
                        "steps": step_count,
                    }

                step_count += 1
                review_notes.append("User signed in / authenticated")
                hooks.emit(f"WorkdayHandler: received user confirmation that sign-in is done ('{answer}'). Waiting for page to update...")
                for _ in range(30):
                    _settle(page, 1000)
                    if not is_sign_in_page(page):
                        break
                _settle(page, 1500)
                hooks.emit("WorkdayHandler: sign-in completed successfully, continuing application.")
                continue

            # ── Phase 1: Job Posting page (click Apply) ───────────────────
            if current_step == "Job Posting":
                hooks.emit("WorkdayHandler: on Job Posting page — clicking Apply")
                apply_btn = page.locator(
                    f'{sel(APPLY_BUTTON)}, '
                    f'{sel("job-posting-apply-button")}, '
                    f'{sel("adventureButton")}, '
                    'button:has-text("Apply"), '
                    'a:has-text("Apply")'
                ).first
                if apply_btn.count() and apply_btn.is_visible():
                    apply_btn.click(timeout=8_000)
                _settle(page, 1500)
                continue

            # ── Phase 2: Apply Options (Autofill vs Manual) ───────────────
            if current_step == "Apply Options":
                hooks.emit("WorkdayHandler: selecting application method")
                has_resume = bool(sess.docs.get("resume"))
                if has_resume:
                    opt = page.locator(
                        f'{sel(APPLY_WITH_RESUME_BUTTON)}, '
                        'button:has-text("Autofill with Resume"), '
                        'a:has-text("Autofill with Resume")'
                    ).first
                    if opt.count() and opt.is_visible():
                        opt.click(timeout=6_000)
                        hooks.emit("WorkdayHandler: clicked 'Autofill with Resume'")
                    else:
                        opt_m = page.locator(
                            f'{sel(APPLY_MANUALLY_BUTTON)}, '
                            'button:has-text("Apply Manually"), '
                            'a:has-text("Apply Manually")'
                        ).first
                        if opt_m.count() and opt_m.is_visible():
                            opt_m.click(timeout=6_000)
                            hooks.emit("WorkdayHandler: clicked 'Apply Manually'")
                else:
                    opt_m = page.locator(
                        f'{sel(APPLY_MANUALLY_BUTTON)}, '
                        'button:has-text("Apply Manually"), '
                        'a:has-text("Apply Manually")'
                    ).first
                    if opt_m.count() and opt_m.is_visible():
                        opt_m.click(timeout=6_000)
                        hooks.emit("WorkdayHandler: clicked 'Apply Manually'")
                _settle(page, 1500)
                continue

            # ── Phase 4: Autofill with Resume upload page ─────────────────
            if current_step == "Autofill with Resume":
                hooks.emit("WorkdayHandler: handling 'Autofill with Resume' upload page")
                resume_path = sess.docs.get("resume")
                if resume_path and Path(resume_path).is_file():
                    file_inputs = page.locator('input[type="file"]')
                    if file_inputs.count():
                        file_inputs.first.set_input_files(str(resume_path))
                        hooks.emit("WorkdayHandler: attached resume file")
                        page.wait_for_timeout(2000)
                # Click Continue / Next
                cont_btn = page.locator(
                    f'{sel(NEXT_BUTTON)}, '
                    f'{sel(AUTOFILL_RESUME_CONTINUE)}, '
                    'button:has-text("Continue"), '
                    'button:has-text("Next")'
                ).first
                if cont_btn.count() and cont_btn.is_visible():
                    cont_btn.click(timeout=8000)
                    hooks.emit("WorkdayHandler: clicked Continue after uploading resume")
                try:
                    page.wait_for_selector(f'{sel(LOADING_SPINNER)}, [class*="loading"]', state="detached", timeout=25000)
                except Exception:
                    pass
                _settle(page, 2500)
                continue

            # ── Phase 5: Reached the Review page ──────────────────────────
            if current_step == "Review" or is_present(page, REVIEW_SECTION, 2_000):
                hooks.emit("WorkdayHandler: reached Review page")
                rev = review_step.fill(sess, hooks, profile)
                blanks = rev.get("blank_required", [])

                try:
                    snapshot(sess, path=run_dir / "review.jpg", full_page=True)
                except Exception:
                    pass

                return {
                    "status": "ready_for_review",
                    "summary": (
                        "Workday application completed deterministically. "
                        + (f"AI was used for: {', '.join(review_notes)}. " if review_notes else "")
                        + "Ready for final review and submit."
                    ),
                    "review_notes": review_notes,
                    "blank_required": blanks,
                    "steps": step_count,
                }

            # ── Detect confirmation page (already submitted) ───────────────
            page_text = ""
            try:
                page_text = page.locator("body").inner_text()[:500].casefold()
            except Exception:
                pass
            if any(kw in page_text for kw in ("thank you for applying", "application submitted", "application received")):
                hooks.emit("WorkdayHandler: confirmation page detected")
                return {
                    "status": "confirmation_seen",
                    "summary": "Application submitted — confirmation page detected.",
                    "review_notes": review_notes,
                    "blank_required": [],
                    "steps": step_count,
                }

            # ── Dispatch to the appropriate step handler ──────────────────
            result: dict = {}
            if current_step == "My Information":
                result = my_information.fill(sess, hooks, profile)
            elif current_step == "My Experience":
                result = my_experience.fill(sess, hooks, profile)
            elif current_step == "Application Questions":
                result = application_questions.fill(sess, hooks, profile)
            elif current_step == "Voluntary Disclosures":
                result = voluntary_disclosures.fill(sess, hooks, profile)
            else:
                # Custom/unknown step — run AI fallback for this page
                log.warning("WorkdayHandler: unknown step %r — using AI fallback", current_step)
                result = {"fallback": True}

            step_count += 1

            # ── Handle step result ────────────────────────────────────────
            if result.get("needs_input"):
                return {
                    "status": "needs_input",
                    "summary": result.get("question", "User input required"),
                    "review_notes": review_notes,
                    "blank_required": [],
                    "question": result.get("question", ""),
                    "choices": result.get("choices", []),
                    "steps": step_count,
                }

            if result.get("fallback"):
                # Field-level AI fallback: run AI only for this page
                label = result.get("step_label") or current_step
                review_notes.append(f"AI handled '{label}' step")
                hooks.emit(f"WorkdayHandler: AI fallback for step '{label}'")

                ai_result = _run_ai_for_page(sess, task, hooks, run_dir, label)
                if ai_result["status"] == "stopped":
                    return {**ai_result, "steps": step_count, "review_notes": review_notes}
                if ai_result["status"] == "needs_input":
                    return {**ai_result, "steps": step_count, "review_notes": review_notes}
                # AI finished fields on page — proceed to click Next below

            # ── Advance to the next step ──────────────────────────────────
            page = current_page(sess)
            clicked = _click_next(page)
            if not clicked:
                if is_present(page, REVIEW_SECTION, 3_000):
                    hooks.emit("WorkdayHandler: no Next button, but Review section present")
                    continue
                log.warning("WorkdayHandler: could not click Next on step %r", current_step)
                hooks.emit(f"WorkdayHandler: could not advance from '{current_step}' — using AI fallback")
                return self._full_ai_fallback(sess, hooks, run_dir, task, step_count, review_notes)

            _settle(page)

            # ── Detect validation errors ──────────────────────────────────
            page = current_page(sess)
            error_banner = maybe_wait(page, "error-alert", 2_000)
            if error_banner:
                err_text = error_banner.inner_text().strip()[:200]
                hooks.emit(f"WorkdayHandler: validation error on '{current_step}': {err_text}")
                log.warning("  validation error: %s", err_text)
                ai_result = _run_ai_for_page(sess, task, hooks, run_dir, current_step)
                review_notes.append(f"AI fixed validation error on '{current_step}'")
                if ai_result["status"] in ("stopped", "needs_input"):
                    return {**ai_result, "steps": step_count, "review_notes": review_notes}
                _click_next(current_page(sess))
                _settle(current_page(sess))

        hooks.emit("WorkdayHandler: exceeded max iterations — using full AI fallback")
        return self._full_ai_fallback(sess, hooks, run_dir, task, step_count, review_notes)

    def _full_ai_fallback(
        self, sess, hooks, run_dir, task: str, steps_so_far: int, review_notes: list
    ) -> dict:
        """Hand off the ENTIRE remaining application to the AI agent."""
        from agent.core import run_agent

        hooks.emit("WorkdayHandler: switching to full AI agent for the remainder")
        review_notes.append("AI handled remainder of form (deterministic flow could not continue)")
        result = run_agent(sess, task, hooks, run_dir)
        result.setdefault("review_notes", [])
        result["review_notes"] = review_notes + (result["review_notes"] or [])
        result["steps"] = steps_so_far + result.get("steps", 0)
        return result
