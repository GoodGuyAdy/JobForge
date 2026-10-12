"""Workday-specific Playwright locator constants and helper functions.

All data-automation-id values and CSS selectors for Workday are centralised
here so that step modules never hard-code them inline.  When Workday updates
its DOM, only this file needs to change.

Workday's SPA uses ``data-automation-id`` as its primary semantic hook.
These IDs are consistent across tenants (companies) on the same Workday
version, although some optional sections vary by employer configuration.

Naming convention
-----------------
Constants are named after their ``data-automation-id`` value with a ``SEL_``
prefix when they are CSS selector strings, and plain uppercase when they are
raw attribute values.

Usage::

    from ats.workday.selectors import sel, wait_for, NEXT_BUTTON

    locator = page.locator(sel("formContainer"))
    next_btn = wait_for(page, NEXT_BUTTON)
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Raw data-automation-id strings
# ---------------------------------------------------------------------------

# ── Navigation ──────────────────────────────────────────────────────────────
NEXT_BUTTON = "bottom-navigation-next-btn"
SAVE_CONTINUE_BUTTON = "bottom-navigation-save-and-continue-btn"
PREVIOUS_BUTTON = "bottom-navigation-previous-btn"
SUBMIT_BUTTON = "bottom-navigation-submit-btn"

# ── Job Details & Apply Modal ───────────────────────────────────────────────
APPLY_BUTTON = "applyButton"
APPLY_WITH_RESUME_BUTTON = "applyWithResume"
APPLY_MANUALLY_BUTTON = "applyManually"
USE_LAST_APPLICATION_BUTTON = "useMyLastApplication"
AUTOFILL_RESUME_CONTINUE = "applyWithResumeContinue"

# The "Save for Later" / "Sign In" flow
SIGN_IN_BUTTON = "signInWidget-signIn"
CREATE_ACCOUNT_BUTTON = "signInWidget-createAccount"
SIGN_IN_SUBMIT_BUTTON = "signInSubmitButton"

# ── Step progress breadcrumb ─────────────────────────────────────────────────
# Workday renders step names in a progress-bar nav.
STEP_INDICATOR_CONTAINER = "stepIndicator"
# Individual step items have data-automation-id like "step-1", "step-2", etc.
# Use step_locator() helper instead of hard-coding these.

# ── Global page state ────────────────────────────────────────────────────────
FORM_CONTAINER = "formContainer"
ERROR_ALERT = "error-alert"           # top-level validation error banner
LOADING_SPINNER = "loadingContainer"

# ── My Information step ──────────────────────────────────────────────────────
LEGAL_NAME_SECTION = "legalName"
FIRST_NAME = "legalNameSection_firstName"
LAST_NAME = "legalNameSection_lastName"
MIDDLE_NAME = "legalNameSection_middleName"

ADDRESS_SECTION = "addressSection"
ADDRESS_LINE1 = "addressSection_addressLine1"
ADDRESS_CITY = "addressSection_city"
ADDRESS_STATE = "addressSection_stateProvince"          # custom dropdown
ADDRESS_POSTAL = "addressSection_postalCode"
ADDRESS_COUNTRY = "addressSection_country"              # custom dropdown

PHONE_SECTION = "phoneSection"
PHONE_DEVICE_TYPE = "phone-device-type"                 # dropdown
PHONE_NUMBER = "phone-number"

EMAIL_SECTION = "emailSection"
EMAIL_ADDRESS = "email"

# "How did you hear about us?"
SOURCE_DROPDOWN = "sourcePromotion"

# Former employee / current employee questions
FORMER_WORKER_RADIO = "isFormerWorker"
CURRENT_WORKER_RADIO = "isCurrentWorker"

# ── My Experience step ───────────────────────────────────────────────────────
# Resume upload
RESUME_UPLOAD_SECTION = "resumeSection"
RESUME_FILE_INPUT = "file-upload-input"                 # <input type="file">
RESUME_DROP_ZONE = "drop-zone"
RESUME_PARSE_BUTTON = "parseResume"

# Work History (may be auto-populated from resume parse)
WORK_HISTORY_SECTION = "workExperienceSection"
ADD_WORK_EXP_BUTTON = "Add Work Experience"             # button text
WORK_EXP_EMPLOYER = "workExperience-employer"
WORK_EXP_TITLE = "workExperience-title"
WORK_EXP_START_MONTH = "workExperience-startDate-month"
WORK_EXP_START_YEAR = "workExperience-startDate-year"
WORK_EXP_END_MONTH = "workExperience-endDate-month"
WORK_EXP_END_YEAR = "workExperience-endDate-year"
WORK_EXP_CURRENT = "workExperience-currentlyWorkHere"  # checkbox
WORK_EXP_DESC = "workExperience-description"
WORK_EXP_LOCATION = "workExperience-location"

# Education
EDUCATION_SECTION = "educationSection"
ADD_EDUCATION_BUTTON = "Add Education"                  # button text
EDUCATION_SCHOOL = "education-school"
EDUCATION_DEGREE = "education-degree"
EDUCATION_FIELD = "education-fieldOfStudy"
EDUCATION_START_MONTH = "education-startDate-month"
EDUCATION_START_YEAR = "education-startDate-year"
EDUCATION_END_MONTH = "education-endDate-month"
EDUCATION_END_YEAR = "education-endDate-year"
EDUCATION_GPA = "education-gpa"

# Skills
SKILLS_SECTION = "skillsSection"
SKILL_INPUT = "multi-select-input"   # searchable skill combobox

# Websites / social media
WEBSITES_SECTION = "websitesSection"
WEBSITE_TYPE = "website-type"
WEBSITE_URL = "website-url"
ADD_WEBSITE_BUTTON = "Add Website"

# ── Application Questions step ───────────────────────────────────────────────
APP_QUESTIONS_SECTION = "applicationQuestions"

# ── Voluntary Disclosures step ───────────────────────────────────────────────
VOLUNTARY_SECTION = "voluntaryDisclosuresSection"
GENDER_DROPDOWN = "questionnaire-gender"
ETHNIC_DROPDOWN = "questionnaire-ethnicity"
VETERAN_DROPDOWN = "questionnaire-veteran"
DISABILITY_DROPDOWN = "questionnaire-disability"

# ── Review step ──────────────────────────────────────────────────────────────
REVIEW_SECTION = "reviewSection"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def sel(automation_id: str) -> str:
    """Return a CSS selector that targets the given data-automation-id."""
    return f'[data-automation-id="{automation_id}"]'


def wait_for(page_or_frame, automation_id: str, timeout: int = 15_000):
    """Wait until an element with *automation_id* is visible and return its locator."""
    loc = page_or_frame.locator(sel(automation_id))
    loc.wait_for(state="visible", timeout=timeout)
    return loc


def maybe_wait(page_or_frame, automation_id: str, timeout: int = 5_000):
    """Return the locator if visible within *timeout*, else None. Never raises."""
    try:
        loc = page_or_frame.locator(sel(automation_id))
        loc.wait_for(state="visible", timeout=timeout)
        return loc
    except Exception:
        return None


def is_present(page_or_frame, automation_id: str, timeout: int = 3_000) -> bool:
    """True if the element exists and is visible."""
    return maybe_wait(page_or_frame, automation_id, timeout) is not None


# ---------------------------------------------------------------------------
# Step detection helpers
# ---------------------------------------------------------------------------

# Workday's step breadcrumb typically contains text like
# "My Information", "My Experience", "Application Questions", etc.
STEP_NAMES = [
    "My Information",
    "My Experience",
    "Application Questions",
    "Voluntary Disclosures",
    "Review",
]

_STEP_PATTERNS = {name: re.compile(re.escape(name), re.IGNORECASE) for name in STEP_NAMES}


def current_step_name(page) -> str | None:
    """Read the active breadcrumb item text from the Workday progress bar.

    Returns one of STEP_NAMES or None if the progress bar is not found.
    """
    try:
        # Active step is usually marked with aria-current="step" or a selected class.
        active = page.locator(
            f'{sel(STEP_INDICATOR_CONTAINER)} [aria-current="step"], '
            f'{sel(STEP_INDICATOR_CONTAINER)} [aria-current="true"], '
            f'{sel(STEP_INDICATOR_CONTAINER)} [class*="selected"], '
            f'{sel(STEP_INDICATOR_CONTAINER)} [class*="active"], '
            '[aria-current="step"], '
            '[data-automation-id*="step"][aria-current="true"]'
        )
        if active.count():
            text = active.first.inner_text().strip()
            for name in STEP_NAMES:
                if _STEP_PATTERNS[name].search(text):
                    return name

        # Check main headings (h1, h2, h3) for standard step titles
        headings = page.locator("h1, h2, h3")
        for i in range(min(headings.count(), 10)):
            h_text = headings.nth(i).inner_text().strip()
            for name in STEP_NAMES:
                if _STEP_PATTERNS[name].search(h_text):
                    return name

        # Fallback: check all step indicator items
        all_steps = page.locator(f'{sel(STEP_INDICATOR_CONTAINER)} [data-automation-id]')
        for i in range(all_steps.count()):
            text = all_steps.nth(i).inner_text().strip()
            for name in STEP_NAMES:
                if _STEP_PATTERNS[name].search(text):
                    pass
    except Exception:
        pass
    return None


def detect_step_from_url(url: str) -> str | None:
    """Infer the current Workday step from the URL fragment/path.

    Workday does not always change the URL between steps, but when it does
    the step name typically appears in the hash or query string.
    """
    url_lower = url.lower()
    for name in STEP_NAMES:
        slug = name.lower().replace(" ", "-")
        if slug in url_lower or slug.replace("-", "") in url_lower:
            return name
    return None


def is_sign_in_page(page) -> bool:
    """Return True if the current page is a Workday sign-in or create-account page."""
    try:
        # 1. Any visible password field is an immediate sign-in indicator
        if page.locator('input[type="password"]:visible').count() > 0:
            return True
        # 2. Automation IDs for sign-in widgets
        for aid in (SIGN_IN_BUTTON, CREATE_ACCOUNT_BUTTON, SIGN_IN_SUBMIT_BUTTON):
            if is_present(page, aid, 500):
                return True
        if page.locator('[data-automation-id="signInWidget-signIn"]:visible').count() > 0:
            return True
        if page.locator('[data-automation-id="signInWidget-email"]:visible').count() > 0:
            return True
        # 3. URL path indicators combined with login fields
        url_lower = page.url.lower()
        if any(kw in url_lower for kw in ("/login", "/auth", "/signin", "login", "signinwidget")):
            if page.locator('input[type="password"]').count() > 0 or page.locator('input[type="email"]').count() > 0:
                return True
        # 4. Heading or button indicators combined with email field
        if page.locator('input[type="email"]:visible').count() > 0:
            if page.locator('button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Create Account")').count() > 0:
                return True
    except Exception:
        pass
    return False


__all__ = [
    # Navigation IDs
    "NEXT_BUTTON", "SAVE_CONTINUE_BUTTON", "PREVIOUS_BUTTON", "SUBMIT_BUTTON",
    "APPLY_BUTTON", "APPLY_WITH_RESUME_BUTTON", "APPLY_MANUALLY_BUTTON",
    "USE_LAST_APPLICATION_BUTTON", "AUTOFILL_RESUME_CONTINUE",
    "SIGN_IN_BUTTON", "CREATE_ACCOUNT_BUTTON", "SIGN_IN_SUBMIT_BUTTON",
    # My Information
    "LEGAL_NAME_SECTION", "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME",
    "ADDRESS_SECTION", "ADDRESS_LINE1", "ADDRESS_CITY", "ADDRESS_STATE",
    "ADDRESS_POSTAL", "ADDRESS_COUNTRY",
    "PHONE_SECTION", "PHONE_DEVICE_TYPE", "PHONE_NUMBER",
    "EMAIL_SECTION", "EMAIL_ADDRESS",
    "SOURCE_DROPDOWN", "FORMER_WORKER_RADIO", "CURRENT_WORKER_RADIO",
    # My Experience
    "RESUME_UPLOAD_SECTION", "RESUME_FILE_INPUT", "RESUME_DROP_ZONE",
    "WORK_HISTORY_SECTION", "WORK_EXP_EMPLOYER", "WORK_EXP_TITLE",
    "EDUCATION_SECTION", "EDUCATION_SCHOOL", "EDUCATION_DEGREE",
    "SKILLS_SECTION", "SKILL_INPUT",
    # Other steps
    "APP_QUESTIONS_SECTION", "VOLUNTARY_SECTION", "REVIEW_SECTION",
    # Utilities
    "sel", "wait_for", "maybe_wait", "is_present", "is_sign_in_page",
    "STEP_NAMES", "current_step_name", "detect_step_from_url",
]
