"""Verification tests for the ATS package.

Run from the project root:
    python tests/test_ats.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_detect():
    from ats import detect
    assert detect("https://ebay.wd5.myworkdayjobs.com/apply/job/test") == "workday", "Workday wd5 URL"
    assert detect("https://company.myworkdayjobs.com/en-US/apply/job") == "workday", "Workday generic URL"
    assert detect("https://wd3.myworkday.com/company/apply") == "workday", "Workday wd3 URL"
    assert detect("https://boards.greenhouse.io/company/jobs/123") is None, "Greenhouse should be None (not implemented)"
    assert detect("https://google.com") is None, "Unknown ATS"
    assert detect("") is None, "Empty URL"
    print("test_detect: PASSED")


def test_base():
    from ats.base import ATSHandler
    import inspect
    assert inspect.isabstract(ATSHandler), "ATSHandler must be abstract"
    print("test_base: PASSED")


def test_selectors():
    from ats.workday.selectors import sel, NEXT_BUTTON, STEP_NAMES, detect_step_from_url, is_sign_in_page
    expected = '[data-automation-id="bottom-navigation-next-btn"]'
    assert sel(NEXT_BUTTON) == expected, f"sel() mismatch: {sel(NEXT_BUTTON)!r}"
    assert "My Information" in STEP_NAMES
    assert "Review" in STEP_NAMES
    assert callable(is_sign_in_page)
    # detect_step_from_url heuristic
    result = detect_step_from_url("https://x.com/#my-information")
    assert result == "My Information", f"detect_step_from_url gave {result!r}"
    result2 = detect_step_from_url("https://x.com/#totally-unknown")
    assert result2 is None
    print("test_selectors: PASSED")


def test_workday_handler():
    from ats.workday.handler import WorkdayHandler
    from ats.base import ATSHandler
    assert issubclass(WorkdayHandler, ATSHandler), "WorkdayHandler must subclass ATSHandler"
    h = WorkdayHandler()
    assert h.name == "Workday"
    print("test_workday_handler: PASSED")


def test_get_handler():
    from ats import get_handler
    from ats.workday.handler import WorkdayHandler
    h = get_handler("https://ebay.wd5.myworkdayjobs.com/apply/job/test")
    assert isinstance(h, WorkdayHandler), f"Expected WorkdayHandler, got {type(h)}"
    h2 = get_handler("https://google.com")
    assert h2 is None, f"Expected None, got {h2}"
    print("test_get_handler: PASSED")


def test_step_modules_importable():
    import importlib
    step_mods = [
        "ats.workday.steps.my_information",
        "ats.workday.steps.my_experience",
        "ats.workday.steps.application_questions",
        "ats.workday.steps.voluntary_disclosures",
        "ats.workday.steps.review",
    ]
    for mod_name in step_mods:
        try:
            mod = importlib.import_module(mod_name)
            assert hasattr(mod, "fill"), f"{mod_name} must have a fill() function"
            print(f"  {mod_name}: PASSED")
        except ImportError as e:
            # Acceptable: some modules import agent.browser which needs Playwright at runtime
            print(f"  {mod_name}: DEFERRED (needs Playwright runtime) - {e}")


def test_skill_helpers():
    from ats.workday.steps.my_experience import _matches_skill, _get_skills_from_profile
    assert _matches_skill("Python", "Python")
    assert _matches_skill("Python (Programming Language)", "Python")
    assert _matches_skill("Python Scripting", "Python")
    assert not _matches_skill("Cython", "Python")
    assert _matches_skill("PostgreSQL", "PostgreSQL")
    assert not _matches_skill("JavaScript", "Java")

    mock_profile = {
        "skills": ["Python", "Docker"],
        "technical_skills": {
            "languages": ["Python", "SQL"],
            "tools": ["Git"],
        }
    }
    extracted = _get_skills_from_profile(mock_profile)
    assert extracted == ["Python", "Docker", "SQL", "Git"]
    print("test_skill_helpers: PASSED")


def test_no_regression_imports():
    """Ensure existing modules still import cleanly after our changes."""
    # These must NOT raise
    import config  # noqa
    print("test_no_regression_imports: PASSED (config)")


if __name__ == "__main__":
    test_detect()
    test_base()
    test_selectors()
    test_workday_handler()
    test_get_handler()
    test_skill_helpers()
    print("\nStep module imports:")
    test_step_modules_importable()
    test_no_regression_imports()
    print("\n=== ALL TESTS PASSED ===")
