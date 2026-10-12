"""Abstract base class for all ATS-specific handlers.

Each ATS module must subclass ATSHandler and implement run().
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ATSHandler(ABC):
    """Contract that every ATS handler must satisfy.

    The handler is responsible for running the entire application flow
    (deterministically where possible, falling back to the AI agent for
    anything unknown).

    Parameters passed to run() are the same objects already available in
    the existing agent/autopilot layer so that no new dependencies are
    introduced.
    """

    # Human-readable name used in logs and status messages.
    name: str = "Unknown ATS"

    @abstractmethod
    def run(self, sess, hooks, run_dir, task: str) -> dict:
        """Execute the full application flow for this ATS.

        Args:
            sess:      agent.browser.Session  — the live browser session.
            hooks:     agent.core.Hooks       — ask/stopped/emit callbacks.
            run_dir:   pathlib.Path           — directory for screenshots/logs.
            task:      str                    — original APPLY_TASK description.

        Returns:
            A result dict with at least:
              {
                "status": "ready_for_review" | "confirmation_seen"
                          | "cannot_continue" | "needs_input" | "stopped",
                "summary": str,
                "review_notes": list[str],   # optional
                "blank_required": list[str], # optional
                "steps": int,
              }
        """

    def is_supported_url(self, url: str) -> bool:  # noqa: D102
        """Quick pre-check (optional override). Always returns True by default."""
        return True


__all__ = ["ATSHandler"]
