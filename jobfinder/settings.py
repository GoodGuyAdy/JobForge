"""Persistence for user-editable settings: saved search criteria and the target company list.
Both are plain JSON files edited by hand (companies.json) or via the /jobs page (criteria.json)."""
import json

import config

from .models import Criteria


def load_criteria() -> Criteria:
    try:
        return Criteria.model_validate_json(config.CRITERIA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return Criteria()


def save_criteria(c: Criteria) -> None:
    config.CRITERIA_PATH.write_text(c.model_dump_json(indent=2), encoding="utf-8")


def load_companies() -> list:
    """Each entry: {"name": "Acme", "board": "greenhouse"|"lever", "slug": "acme"}. See companies.example.json."""
    try:
        return json.loads(config.COMPANIES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
