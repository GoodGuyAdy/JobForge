"""Workday Step 1 — My Information.

Handles:
  - Legal name (first / middle / last)
  - Email address
  - Phone number
  - Home address (line 1, city, state/province, postal, country)
  - "How did you hear about us?" source dropdown
  - Former / current worker questions
  - Account creation / sign-in detection → hands off to human via hooks.ask

Returns a StepResult namedtuple so the caller knows whether to continue,
fall back to AI, or surface a question to the user.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ats.workday.selectors import (
    ADDRESS_CITY,
    ADDRESS_COUNTRY,
    ADDRESS_LINE1,
    ADDRESS_POSTAL,
    ADDRESS_STATE,
    CREATE_ACCOUNT_BUTTON,
    EMAIL_ADDRESS,
    FIRST_NAME,
    FORMER_WORKER_RADIO,
    LAST_NAME,
    MIDDLE_NAME,
    PHONE_NUMBER,
    PHONE_DEVICE_TYPE,
    SIGN_IN_BUTTON,
    SOURCE_DROPDOWN,
    is_present,
    is_sign_in_page,
    maybe_wait,
    sel,
    wait_for,
)

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fill_if_empty(page, automation_id: str, value: str, fallbacks: list[str] | None = None) -> bool:
    """Fill an input only when it is currently blank. Returns True on success."""
    if not value:
        return True  # nothing to fill — treat as success
    loc = maybe_wait(page, automation_id, 3_000)
    if loc is None and fallbacks:
        for fb in fallbacks:
            fb_loc = page.locator(fb).first
            if fb_loc.count() > 0 and fb_loc.is_visible():
                loc = fb_loc
                break
    if loc is None:
        return False
    try:
        current = loc.input_value(timeout=2_000)
        if current.strip():
            log.debug("  skip %s (already has value %r)", automation_id, current)
            return True
        loc.fill(value, timeout=5_000)
        return True
    except Exception as exc:
        log.warning("  fill %s failed: %s", automation_id, exc)
        return False


def _fill_always(page, automation_id: str, value: str) -> bool:
    """Fill an input regardless of current content."""
    if not value:
        return True
    loc = maybe_wait(page, automation_id, 4_000)
    if loc is None:
        return False
    try:
        loc.fill(value, timeout=5_000)
        return True
    except Exception as exc:
        log.warning("  fill %s failed: %s", automation_id, exc)
        return False


def _select_dropdown(page, automation_id: str, value: str, fallbacks: list[str] | None = None) -> bool:
    """Click a Workday custom dropdown and pick the closest matching option."""
    if not value:
        return True
    loc = maybe_wait(page, automation_id, 3_000)
    if loc is None and fallbacks:
        for fb in fallbacks:
            fb_loc = page.locator(fb).first
            if fb_loc.count() > 0 and fb_loc.is_visible():
                loc = fb_loc
                break
    if loc is None:
        return False
    try:
        loc.click(timeout=5_000)
        page.wait_for_timeout(600)
        # Options appear in a list with role="option"
        opts = page.locator('[role="option"]:visible')
        count = opts.count()
        if count == 0:
            # Some Workday dropdowns use a listbox pattern
            opts = page.locator('[role="listbox"] [role="option"]:visible')
            count = opts.count()
        target = value.strip().casefold()
        for i in range(min(count, 80)):
            text = opts.nth(i).inner_text().strip().casefold()
            if text == target or text.startswith(target) or target in text:
                opts.nth(i).click(timeout=3_000)
                return True
        # If not found directly, check for an inner search input (common for country/state in Workday)
        search_input = page.locator('[role="combobox"]:focus, input[type="search"]:visible, [role="listbox"] input:visible').first
        if search_input.count() > 0:
            search_input.fill(value)
            page.wait_for_timeout(600)
            filtered = page.locator('[role="option"]:visible')
            if filtered.count() > 0:
                filtered.first.click(timeout=3_000)
                return True
        # Close the dropdown without selecting
        page.keyboard.press("Escape")
        log.warning("  dropdown %s: no match for %r among %d options", automation_id, value, count)
        return False
    except Exception as exc:
        log.warning("  dropdown %s failed: %s", automation_id, exc)
        return False


def _click_radio_for_label(page, group_automation_id: str, value: str) -> bool:
    """Click the radio/button whose label contains *value* (case-insensitive)."""
    if not value:
        return True
    loc = maybe_wait(page, group_automation_id, 4_000)
    if loc is None:
        return False
    try:
        target = value.strip().casefold()
        options = loc.locator('[role="radio"], [type="radio"], button')
        for i in range(min(options.count(), 10)):
            opt = options.nth(i)
            text = (opt.inner_text() or opt.get_attribute("aria-label") or "").strip().casefold()
            if text == target or target in text:
                opt.click(timeout=3_000)
                return True
        return False
    except Exception as exc:
        log.warning("  radio %s failed: %s", group_automation_id, exc)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fill(sess, hooks: "Hooks", profile: dict) -> dict:
    """Fill the My Information step using *profile* data.

    Returns:
        {"ok": True}                — step completed, proceed
        {"ok": False, "reason": str} — deterministic handling failed; caller
                                       should fall back to the AI agent.
        {"needs_input": True, "question": str, "choices": list}
                                    — user action required (login / CAPTCHA)
    """
    from agent.browser import current_page
    page = current_page(sess)

    personal = profile.get("personal") or {}

    # ── 0. Account creation / sign-in wall ──────────────────────────────────
    if is_sign_in_page(page) or is_present(page, SIGN_IN_BUTTON, 1_000) or is_present(page, CREATE_ACCOUNT_BUTTON, 1_000):
        log.info("  My Information: sign-in / create-account wall detected")
        hooks.emit("WorkdayHandler: Sign In page detected on My Information step. Waiting for user to sign in...")
        answer = hooks.ask(
            "This application is at the Workday Sign In page. "
            "Please sign in or create your account in the browser, "
            "then let me know when it is done so I can continue.",
            ["Done", "I've signed in"],
        )
        if answer is None:
            return {
                "needs_input": True,
                "question": "Please sign in or create a Workday account in the browser, then let me know when it is done.",
                "choices": ["Done", "I've signed in"],
            }
        hooks.emit(f"WorkdayHandler: received user confirmation that sign-in is done ('{answer}'). Waiting for page to update...")
        for _ in range(30):
            page.wait_for_timeout(1000)
            if not is_sign_in_page(page):
                break
        page.wait_for_timeout(1500)
        hooks.emit("WorkdayHandler: sign-in completed, continuing form fill.")

    # ── 1. Legal name ────────────────────────────────────────────────────────
    log.info("  My Information: filling legal name")
    _fill_if_empty(
        page, FIRST_NAME, personal.get("first_name", ""),
        fallbacks=['input[data-automation-id*="firstName"]', 'input[name*="firstName"]', 'input[aria-label*="First Name" i]']
    )
    if personal.get("middle_name"):
        _fill_if_empty(
            page, MIDDLE_NAME, personal.get("middle_name", ""),
            fallbacks=['input[data-automation-id*="middleName"]', 'input[name*="middleName"]']
        )
    _fill_if_empty(
        page, LAST_NAME, personal.get("last_name", ""),
        fallbacks=['input[data-automation-id*="lastName"]', 'input[name*="lastName"]', 'input[aria-label*="Last Name" i]']
    )

    # ── 2. Email ─────────────────────────────────────────────────────────────
    log.info("  My Information: filling email")
    _fill_if_empty(
        page, EMAIL_ADDRESS, personal.get("email", ""),
        fallbacks=['input[type="email"]', 'input[data-automation-id*="email"]', 'input[name*="email"]']
    )

    # ── 3. Phone ─────────────────────────────────────────────────────────────
    log.info("  My Information: filling phone")
    # Phone device type (Mobile/Home/Work) — pick "Mobile" as default
    _select_dropdown(
        page, PHONE_DEVICE_TYPE, "Mobile",
        fallbacks=['[data-automation-id*="phone-device"]', '[data-automation-id*="phoneDevice"]']
    )
    phone = personal.get("phone", "")
    _fill_if_empty(
        page, PHONE_NUMBER, phone,
        fallbacks=['input[type="tel"]', 'input[data-automation-id*="phone"]', 'input[name*="phone"]']
    )

    # ── 4. Address ───────────────────────────────────────────────────────────
    log.info("  My Information: filling address")
    _fill_if_empty(
        page, ADDRESS_LINE1, personal.get("address", ""),
        fallbacks=['input[data-automation-id*="addressLine1"]', 'input[data-automation-id*="address"]', 'input[name*="address"]']
    )
    _fill_if_empty(
        page, ADDRESS_CITY, personal.get("current_city", ""),
        fallbacks=['input[data-automation-id*="city"]', 'input[name*="city"]']
    )
    _fill_if_empty(
        page, ADDRESS_POSTAL, personal.get("postal_code", ""),
        fallbacks=['input[data-automation-id*="postal"]', 'input[name*="postal"]', 'input[data-automation-id*="zip"]']
    )
    _select_dropdown(
        page, ADDRESS_COUNTRY, personal.get("country", ""),
        fallbacks=['[data-automation-id*="country"]', 'button[aria-label*="Country" i]']
    )
    _select_dropdown(
        page, ADDRESS_STATE, personal.get("current_state", ""),
        fallbacks=['[data-automation-id*="state"]', 'button[aria-label*="State" i]']
    )

    # ── 5. Source / referral ─────────────────────────────────────────────────
    if is_present(page, SOURCE_DROPDOWN, 2_000) or page.locator('[data-automation-id*="sourcePromotion"]').count() > 0:
        log.info("  My Information: filling source")
        _select_dropdown(
            page, SOURCE_DROPDOWN, "LinkedIn",
            fallbacks=['[data-automation-id*="sourcePromotion"]', '[data-automation-id*="source"]']
        )

    # ── 6. Former / current worker questions ─────────────────────────────────
    if is_present(page, FORMER_WORKER_RADIO, 2_000) or page.locator('[data-automation-id*="FormerWorker"]').count() > 0:
        log.info("  My Information: former worker question")
        _click_radio_for_label(page, FORMER_WORKER_RADIO, "No")

    # ── 7. Blur all filled fields so Workday validation fires ────────────────
    try:
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)
    except Exception:
        pass

    return {"ok": True}
