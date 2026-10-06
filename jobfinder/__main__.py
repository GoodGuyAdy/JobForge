"""Run one job-search cycle standalone, no dashboard/browser/apply-agent involved.

    python -m jobfinder

Reads the criteria you last saved from the /jobs page (criteria.json); run the app once and use
"Save & search now" first if you haven't saved any criteria yet. Intended for cron / Task Scheduler
instead of leaving JOBS_AUTO_RUN_HOURS running inside the app.
"""
import sys

from . import db, pipeline, settings


def main() -> int:
    db.init()
    criteria = settings.load_criteria()
    summary = pipeline.run_search(criteria, log=print)
    print("SUMMARY:", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
