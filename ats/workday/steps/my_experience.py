"""Workday Step 2 — My Experience.

Handles:
  - Resume / CV upload (triggers parse if Workday offers it)
  - Work history entries (verifies / fills after resume parse)
  - Education entries
  - Skills (delegates to the existing select_workday_skill action)
  - LinkedIn / website URLs

If resume parsing auto-fills the form, we verify key fields and correct
misparses rather than re-entering everything from scratch.

Returns same dict contract as my_information.fill().
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ats.workday.selectors import (
    ADD_EDUCATION_BUTTON,
    ADD_WORK_EXP_BUTTON,
    EDUCATION_DEGREE,
    EDUCATION_FIELD,
    EDUCATION_END_YEAR,
    EDUCATION_SCHOOL,
    EDUCATION_START_YEAR,
    RESUME_FILE_INPUT,
    RESUME_PARSE_BUTTON,
    RESUME_UPLOAD_SECTION,
    SKILLS_SECTION,
    SKILL_INPUT,
    WEBSITES_SECTION,
    WEBSITE_TYPE,
    WEBSITE_URL,
    WORK_EXP_CURRENT,
    WORK_EXP_EMPLOYER,
    WORK_EXP_END_MONTH,
    WORK_EXP_END_YEAR,
    WORK_EXP_START_MONTH,
    WORK_EXP_START_YEAR,
    WORK_EXP_TITLE,
    is_present,
    maybe_wait,
    sel,
    wait_for,
)

if TYPE_CHECKING:
    from agent.browser import Session
    from agent.core import Hooks

log = logging.getLogger(__name__)

# Month-name → number mapping for Workday's month dropdowns.
_MONTH_MAP = {
    "january": "1", "february": "2", "march": "3", "april": "4",
    "may": "5", "june": "6", "july": "7", "august": "8",
    "september": "9", "october": "10", "november": "11", "december": "12",
}


def _month_number(text: str) -> str:
    """Convert e.g. 'December 2025' → '12', or return '' on failure."""
    parts = text.strip().casefold().split()
    for part in parts:
        if part in _MONTH_MAP:
            return _MONTH_MAP[part]
    return ""


def _year(text: str) -> str:
    """Extract a 4-digit year from a date string, e.g. 'December 2025' → '2025'."""
    import re
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return m.group(0) if m else ""


def _fill_text(page, automation_id: str, value: str) -> bool:
    if not value:
        return True
    loc = maybe_wait(page, automation_id, 4_000)
    if loc is None:
        return False
    try:
        loc.fill("", timeout=3_000)
        loc.fill(value, timeout=5_000)
        return True
    except Exception as exc:
        log.warning("  fill %s failed: %s", automation_id, exc)
        return False


def _select_dropdown(page, automation_id: str, value: str) -> bool:
    if not value:
        return True
    loc = maybe_wait(page, automation_id, 4_000)
    if loc is None:
        return False
    try:
        loc.click(timeout=5_000)
        page.wait_for_timeout(500)
        opts = page.locator('[role="option"]:visible')
        target = value.strip().casefold()
        for i in range(min(opts.count(), 80)):
            text = opts.nth(i).inner_text().strip().casefold()
            if text == target or text.startswith(target) or target in text:
                opts.nth(i).click(timeout=3_000)
                return True
        page.keyboard.press("Escape")
        return False
    except Exception as exc:
        log.warning("  dropdown %s failed: %s", automation_id, exc)
        return False


# ---------------------------------------------------------------------------
# Resume upload
# ---------------------------------------------------------------------------

def _upload_resume(page, docs: dict) -> bool:
    """Upload the resume doc if a file input is available."""
    resume_path = docs.get("resume")
    if not resume_path or not Path(resume_path).is_file():
        log.warning("  My Experience: no resume file available to upload")
        return False

    # Workday hides the real <input type="file"> behind a styled dropzone.
    # Check if a resume is already attached (e.g. from the Autofill with Resume step)
    resume_name = Path(resume_path).name
    try:
        if (
            page.locator(f'{sel(RESUME_UPLOAD_SECTION)} text="{resume_name}"').count() > 0
            or page.locator('[data-automation-id="file-upload-item"]').count() > 0
            or page.locator('[data-automation-id*="uploadedFile"]').count() > 0
        ):
            log.info("  My Experience: resume %s already attached — skipping re-upload", resume_name)
            return True
    except Exception:
        pass

    file_inputs = page.locator('input[type="file"]')
    if file_inputs.count() == 0:
        log.warning("  My Experience: no file input found for resume upload")
        return False

    file_inputs.first.set_input_files(resume_path)
    page.wait_for_timeout(1_500)
    log.info("  My Experience: resume uploaded — waiting for parse")

    # Click "Parse Resume" if the button appears
    parse_btn = maybe_wait(page, RESUME_PARSE_BUTTON, 8_000)
    if parse_btn:
        parse_btn.click(timeout=5_000)
        page.wait_for_timeout(3_000)  # give Workday time to parse

    return True


# ---------------------------------------------------------------------------
# Work experience
# ---------------------------------------------------------------------------

def _fill_work_entry(page, entry: dict, index: int) -> None:
    """Fill / verify a single work experience card at position *index*."""
    # Workday repeats the automation IDs with an index suffix for multiple entries
    suffix = f"_{index}" if index > 0 else ""

    _fill_text(page, WORK_EXP_EMPLOYER + suffix, entry.get("company", ""))
    _fill_text(page, WORK_EXP_TITLE + suffix, entry.get("title", ""))

    start = entry.get("start_date", "")
    _select_dropdown(page, WORK_EXP_START_MONTH + suffix, _month_number(start))
    _fill_text(page, WORK_EXP_START_YEAR + suffix, _year(start))

    end = entry.get("end_date", "")
    if end.strip().casefold() in ("present", "current", ""):
        # Tick "I currently work here"
        chk = maybe_wait(page, WORK_EXP_CURRENT + suffix, 3_000)
        if chk and not chk.is_checked():
            chk.click(timeout=3_000)
    else:
        _select_dropdown(page, WORK_EXP_END_MONTH + suffix, _month_number(end))
        _fill_text(page, WORK_EXP_END_YEAR + suffix, _year(end))


def _ensure_work_history(page, profile: dict) -> None:
    """Add or verify work experience entries from the profile."""
    entries = profile.get("work_experience", [])
    if not entries:
        return

    # Check whether Workday already has entries (e.g. from resume parse)
    existing_cards = page.locator(f'{sel("workExperience")} [data-automation-id*="workExperience-employer"]')
    existing_count = existing_cards.count()
    log.info("  My Experience: %d existing work cards, %d in profile", existing_count, len(entries))

    for i, entry in enumerate(entries):
        if i >= existing_count:
            # Need to click "Add Work Experience" first
            add_btn = page.locator(f'button:has-text("{ADD_WORK_EXP_BUTTON}")')
            if add_btn.count():
                add_btn.first.click(timeout=5_000)
                page.wait_for_timeout(800)
        _fill_work_entry(page, entry, i)


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

def _fill_education_entry(page, entry: dict, index: int) -> None:
    suffix = f"_{index}" if index > 0 else ""

    _fill_text(page, EDUCATION_SCHOOL + suffix, entry.get("institution", ""))
    _select_dropdown(page, EDUCATION_DEGREE + suffix, entry.get("degree", ""))
    _fill_text(page, EDUCATION_FIELD + suffix, entry.get("field_of_study", ""))

    start = entry.get("start_date", "")
    _fill_text(page, EDUCATION_START_YEAR + suffix, _year(start))

    end = entry.get("end_date", "")
    _fill_text(page, EDUCATION_END_YEAR + suffix, _year(end))


def _ensure_education(page, profile: dict) -> None:
    entries = profile.get("education", [])
    if not entries:
        return

    existing_cards = page.locator(f'{sel("education")} [data-automation-id*="education-school"]')
    existing_count = existing_cards.count()

    for i, entry in enumerate(entries):
        if i >= existing_count:
            add_btn = page.locator(f'button:has-text("{ADD_EDUCATION_BUTTON}")')
            if add_btn.count():
                add_btn.first.click(timeout=5_000)
                page.wait_for_timeout(800)
        _fill_education_entry(page, entry, i)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def _get_skills_from_profile(profile: dict) -> list[str]:
    """Extract and deduplicate skills from user profile."""
    skills: list[str] = []
    # 1. Direct skills list if present
    direct = profile.get("skills", [])
    if isinstance(direct, list):
        skills.extend(direct)
    # 2. technical_skills dict / list
    tech = profile.get("technical_skills", {})
    if isinstance(tech, dict):
        for items in tech.values():
            if isinstance(items, list):
                skills.extend(items)
            elif isinstance(items, str):
                skills.append(items)
    elif isinstance(tech, list):
        skills.extend(tech)

    seen = set()
    unique = []
    for s in skills:
        if isinstance(s, str):
            clean = s.strip()
            if clean and clean.casefold() not in seen:
                seen.add(clean.casefold())
                unique.append(clean)
    return unique


def _find_skill_input(page):
    """Find the skill search input in Workday with comprehensive fallbacks."""
    candidates = [
        # Inside skills section container
        f'{sel(SKILLS_SECTION)} input:visible',
        f'{sel(SKILLS_SECTION)} [role="combobox"]:visible',
        # Standard Automation IDs
        f'{sel(SKILL_INPUT)}:visible',
        f'{sel("multiSelectSearchInput")}:visible',
        f'{sel("search-input")}:visible',
        f'{sel("skillsPrompt")} input:visible',
        # Partial automation IDs
        '[data-automation-id*="skill" i] input:visible',
        '[data-automation-id*="Skill"] input:visible',
        '[data-automation-id*="skill" i][role="combobox"]:visible',
        # Placeholders & aria labels
        'input[placeholder*="skill" i]:visible',
        'input[aria-label*="skill" i]:visible',
        '[role="combobox"][aria-label*="skill" i]:visible',
        # Containers with heading / label "Skills"
        'div:has(h2:has-text("Skills"), h3:has-text("Skills"), label:has-text("Skills")) input:visible',
        'section:has-text("Skills") input:visible',
    ]
    for sel_str in candidates:
        try:
            loc = page.locator(sel_str).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _matches_skill(option_text: str, skill: str) -> bool:
    """Check if option text matches the requested skill word."""
    import re
    opt_lower = " ".join(option_text.casefold().split())
    sk_lower = " ".join(skill.casefold().split())
    if not opt_lower or not sk_lower:
        return False
    if opt_lower == sk_lower:
        return True
    pattern = r"\b" + re.escape(sk_lower) + r"\b"
    if re.search(pattern, opt_lower):
        return True
    return False


def _clear_search_bar(page, search_bar) -> None:
    """Clear the skill search bar input."""
    try:
        search_bar.click(timeout=2_000)
        search_bar.fill("")
        search_bar.press("Control+a")
        search_bar.press("Backspace")
        page.wait_for_timeout(200)
    except Exception as exc:
        log.debug("  _clear_search_bar: %s", exc)


def _fill_skills(page, profile: dict, hooks=None) -> None:
    """Iteratively fill skills into Workday's skills selector.

    Workflow:
      1. If an 'Add' or 'Add Skill' button is present, click it.
      2. Locate the skill search bar.
      3. For each skill from user_profile:
         - Clear the search bar.
         - Type the skill name.
         - Press Enter.
         - Wait for the dropdown of matching results.
         - Check mark the relevant option(s) in the dropdown that have the skill word.
         - Clear the search bar.
         - Repeat for the next skill.
      4. Close the dropdown list.
    """
    all_skills = _get_skills_from_profile(profile)
    if not all_skills:
        log.info("  My Experience: no skills found in profile")
        return

    # Check if an 'Add Skill' button needs to be clicked first
    add_btn = page.locator(
        f'{sel(SKILLS_SECTION)} button:has-text("Add"), '
        'button:has-text("Add Skill"), '
        'button:has-text("Add Skills")'
    ).first
    if add_btn.count() > 0 and add_btn.is_visible():
        try:
            add_btn.click(timeout=3_000)
            page.wait_for_timeout(600)
        except Exception:
            pass

    search_bar = _find_skill_input(page)
    if search_bar is None:
        log.info("  My Experience: no skill search bar found on page")
        return

    log.info("  My Experience: skill search bar found — filling %d skills", len(all_skills[:25]))
    if hooks:
        hooks.emit("Workday: adding skills from profile...")

    selected_total = 0

    for skill in all_skills[:25]:  # select up to 25 skills from profile
        # Re-locate search bar on each iteration (Workday re-renders inputs as tags are added)
        search_bar = _find_skill_input(page)
        if search_bar is None:
            log.warning("  My Experience: lost skill search bar, stopping skills loop")
            break

        try:
            search_bar.scroll_into_view_if_needed(timeout=3_000)
            _clear_search_bar(page, search_bar)

            # Type skill into search bar
            search_bar.fill(skill)
            page.wait_for_timeout(300)

            # Press Enter
            search_bar.press("Enter")
            page.wait_for_timeout(1_000)

            # Look for dropdown options
            option_selectors = [
                '[role="listbox"]:visible [role="option"]:visible',
                '[role="option"]:visible',
                '[data-automation-id*="promptOption"]:visible',
                '[role="listbox"]:visible [role="checkbox"]:visible',
                '[role="checkbox"]:visible',
            ]
            options = None
            for sel_candidate in option_selectors:
                loc = page.locator(sel_candidate)
                if loc.count() > 0:
                    options = loc
                    break

            if options and options.count() > 0:
                matched_any = False
                for i in range(min(options.count(), 20)):
                    opt = options.nth(i)
                    opt_text = opt.inner_text().strip()
                    if _matches_skill(opt_text, skill):
                        # Check if already selected
                        is_checked = (
                            opt.get_attribute("aria-checked") == "true"
                            or opt.get_attribute("aria-selected") == "true"
                            or opt.locator('[aria-checked="true"]').count() > 0
                            or opt.locator('input[type="checkbox"]:checked').count() > 0
                        )
                        if not is_checked:
                            # Check mark the option
                            chk = opt.locator('[role="checkbox"], input[type="checkbox"]').first
                            if chk.count() > 0 and chk.is_visible():
                                chk.click(timeout=3_000)
                            else:
                                opt.click(timeout=3_000)
                            selected_total += 1
                            matched_any = True
                            log.info("  My Experience: check marked skill %r (search: %r)", opt_text, skill)
                            if hooks:
                                hooks.emit(f"Workday: check marked skill '{opt_text}'")
                            page.wait_for_timeout(400)
                            break  # Move to next skill once matched

                if not matched_any:
                    log.info("  My Experience: no unselected match for %r in %d options", skill, options.count())
            else:
                log.info("  My Experience: no dropdown options appeared after Enter for %r", skill)

            # Clear search bar before the next iteration
            search_bar = _find_skill_input(page)
            if search_bar:
                _clear_search_bar(page, search_bar)

        except Exception as exc:
            log.warning("  My Experience: error processing skill %r: %s", skill, exc)
            continue

    # Close dropdown list when finished
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.keyboard.press("Tab")
    except Exception:
        pass

    log.info("  My Experience: finished skills — check marked %d total skills", selected_total)
    if hooks:
        hooks.emit(f"Workday: completed adding skills ({selected_total} selected)")


# ---------------------------------------------------------------------------
# Social / website links
# ---------------------------------------------------------------------------

def _fill_websites(page, profile: dict) -> None:
    personal = profile.get("personal") or {}
    links = [
        ("LinkedIn Profile", personal.get("linkedin", "")),
        ("Portfolio/Personal Website", personal.get("github", "")),
    ]
    for link_type, url in links:
        if not url:
            continue
        if not is_present(page, WEBSITES_SECTION, 2_000):
            break
        add_btn = page.locator(f'button:has-text("Add Website")')
        if add_btn.count():
            add_btn.first.click(timeout=5_000)
            page.wait_for_timeout(500)
        _select_dropdown(page, WEBSITE_TYPE, link_type)
        _fill_text(page, WEBSITE_URL, url)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fill(sess, hooks: "Hooks", profile: dict) -> dict:
    """Fill the My Experience step using *profile* data.

    Strategy:
    1. Upload the resume → Workday parses it and pre-fills many fields.
    2. Verify / correct / add work experience entries.
    3. Verify / correct / add education entries.
    4. Add skills.
    5. Add website links (LinkedIn, GitHub).
    """
    from agent.browser import current_page
    page = current_page(sess)

    # ── 1. Resume upload ─────────────────────────────────────────────────────
    if is_present(page, RESUME_UPLOAD_SECTION, 3_000):
        log.info("  My Experience: uploading resume")
        _upload_resume(page, sess.docs)
        # Wait for Workday's parser to run before we try to fill anything else
        page.wait_for_timeout(2_000)

    # ── 2. Work experience ────────────────────────────────────────────────────
    log.info("  My Experience: verifying work history")
    _ensure_work_history(page, profile)

    # ── 3. Education ─────────────────────────────────────────────────────────
    log.info("  My Experience: verifying education")
    _ensure_education(page, profile)

    # ── 4. Skills ────────────────────────────────────────────────────────────
    log.info("  My Experience: adding skills")
    _fill_skills(page, profile, hooks)

    # ── 5. Websites ──────────────────────────────────────────────────────────
    log.info("  My Experience: adding website links")
    _fill_websites(page, profile)

    return {"ok": True}
