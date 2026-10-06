import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.browser import (
    Session,
    _calendar_date_text_variants,
    _find_calendar_date,
    _parse_calendar_date,
    a_select_date,
    a_select_workday_skill,
    a_upload_file,
    is_adzuna_url,
)
from agent.core import TOOLS
from config import DEFAULT_ANSWER_RULES


class AgentImprovementsTests(unittest.TestCase):
    def test_calendar_date_variants_support_workday_labels(self):
        target = _parse_calendar_date("2026-12-04")

        variants = _calendar_date_text_variants(target)

        self.assertIn("December 4, 2026", variants)
        self.assertIn("Dec 4, 2026", variants)
        self.assertIn("December 4 2026", variants)
        self.assertIn("4 December 2026", variants)
        self.assertIn("2026-12-04", variants)

    def test_calendar_date_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            _parse_calendar_date("12/04/2026")

    def test_calendar_date_uses_current_grid_month_context(self):
        target = _parse_calendar_date("2026-12-04")
        html = """
          <div role="grid" aria-label="November 2026">
            <button>4</button>
          </div>
          <div role="grid" aria-label="December 2026">
            <button>4</button>
          </div>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            match = _find_calendar_date(page, target)
            selected_grid = match.locator("xpath=ancestor::*[@role='grid'][1]")
            selected_month = selected_grid.get_attribute("aria-label")
            browser.close()

        self.assertEqual("December 2026", selected_month)

    def test_select_workday_skill_retries_with_enter(self):
        html = """
          <div id="skills">
            <input
              id="skill-search"
              data-aid="1"
              role="combobox"
              aria-autocomplete="list"
              aria-haspopup="listbox"
            >
            <div id="results" role="listbox" hidden></div>
          </div>
          <style>
            #results { display: block; }
            #results option { display: block; width: 200px; padding: 8px; }
          </style>
          <script>
            const search = document.getElementById('skill-search');
            const results = document.getElementById('results');
            search.addEventListener('keydown', (event) => {
              if (event.key === 'Enter') {
                results.innerHTML = '<div role="option" aria-selected="false">Python</div>';
                results.hidden = false;
              }
            });
            results.addEventListener('click', (event) => {
              if (event.target.matches('[role="option"]')) {
                event.target.setAttribute('aria-selected', 'true');
              }
            });
          </script>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_content(html)
            sess = Session(context=context, docs={})
            sess.frames = {1: page}
            sess.elements = {
                1: {
                    "tag": "input",
                    "type": "text",
                    "role": "combobox",
                    "label": "Skills",
                    "value": "",
                }
            }
            result = a_select_workday_skill(sess, 1, "Python")
            selected_value = page.locator("#skill-search").input_value()
            selected_state = page.locator('[role="option"]').get_attribute("aria-selected")
            browser.close()

        self.assertIn("selected 'Python'", result)
        self.assertEqual("Python", selected_value)
        self.assertEqual("true", selected_state)

    def test_upload_file_validates_actual_file_input(self):
        html = """
          <button id="resume-button" type="button">Select file</button>
          <input id="resume-file" type="file" hidden>
        """
        path = Path(__file__).with_name("resume.pdf")
        path.write_bytes(b"test resume")

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.set_content(html)
                sess = Session(context=context, docs={"resume": str(path)})
                sess.frames = {1: page}
                sess.elements = {
                    1: {
                        "tag": "button",
                        "type": "button",
                        "role": "",
                        "label": "Select file",
                        "value": "",
                    }
                }
                result = a_upload_file(sess, 1, "resume")
                uploaded_name = page.locator("#resume-file").evaluate(
                    "input => input.files[0]?.name"
                )
                browser.close()
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("uploaded resume", result)
        self.assertEqual("resume.pdf", uploaded_name)

    def test_select_date_falls_back_to_visible_calendar_control(self):
        html = """
          <button data-aid="1" aria-label="Calendar" id="stale-calendar" hidden>Calendar</button>
          <button aria-label="Calendar" id="live-calendar">Calendar</button>
          <div role="grid" aria-label="December 2026">
            <button aria-label="December 4, 2026">4</button>
          </div>
          <script>
            document.querySelector('#live-calendar').addEventListener('click', () => {
              document.querySelector('[aria-label="December 4, 2026"]')
                .setAttribute('aria-selected', 'true');
            });
          </script>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            sess = Session(context=browser.new_context(), docs={})
            sess.job_pages = [page]
            sess.frames = {1: page}
            sess.elements = {1: {"tag": "button", "label": "Calendar"}}
            result = a_select_date(sess, 1, "2026-12-04")
            browser.close()

        self.assertIn("selected 2026-12-04", result)

    def test_agent_exposes_single_call_and_targeted_actions(self):
        tool_names = {tool["name"] for tool in TOOLS}

        self.assertIn("scroll_to", tool_names)
        self.assertIn("select_date", tool_names)
        self.assertIn("select_workday_skill", tool_names)
        self.assertNotIn("scroll", tool_names)

        scroll_to = next(tool for tool in TOOLS if tool["name"] == "scroll_to")
        select_date = next(tool for tool in TOOLS if tool["name"] == "select_date")
        skill = next(tool for tool in TOOLS if tool["name"] == "select_workday_skill")
        self.assertEqual(["id"], scroll_to["parameters"]["required"])
        self.assertEqual(["id", "date"], select_date["parameters"]["required"])
        self.assertEqual(["id", "skill"], skill["parameters"]["required"])

    def test_adzuna_domain_detection_is_host_specific(self):
        self.assertTrue(is_adzuna_url("https://www.adzuna.in/details/5899834514"))
        self.assertTrue(is_adzuna_url("https://jobs.adzuna.in/active/123"))
        self.assertFalse(is_adzuna_url("https://adzuna.in.evil.example/jobs/123"))
        self.assertFalse(is_adzuna_url("https://example.com/jobs/123"))

    def test_generic_compliance_answers_have_safe_defaults(self):
        self.assertIn("able to work in the stated region", DEFAULT_ANSWER_RULES)
        self.assertIn("answer Yes", DEFAULT_ANSWER_RULES)
        self.assertIn("non-compete", DEFAULT_ANSWER_RULES)
        self.assertIn("answer No", DEFAULT_ANSWER_RULES)
        self.assertIn("legal declarations", DEFAULT_ANSWER_RULES)

if __name__ == "__main__":
    unittest.main()
