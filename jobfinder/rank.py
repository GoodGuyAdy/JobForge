"""LLM fit-scoring: extracts work mode / experience / salary / location eligibility from a posting's
text and scores how well it matches the candidate. One stateless call per job (cache-friendly prefix:
instructions + profile are identical across calls; only the posting changes)."""
import json

from openai import OpenAI

import config

from .models import Criteria

client = OpenAI()  # reads OPENAI_API_KEY (loaded from .env by config)

_TOOL = {
    "type": "function", "name": "assess_job", "strict": False,
    "description": "Extract structured facts from a job posting and score how well it fits the candidate.",
    "parameters": {
        "type": "object",
        "properties": {
            "work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
            "exp_min": {"type": ["number", "null"], "description": "Minimum years of experience REQUIRED, if stated"},
            "exp_max": {"type": ["number", "null"], "description": "Maximum years of experience accepted, if stated"},
            "salary_min_lpa": {"type": ["number", "null"], "description": "Minimum annual pay in LPA, if stated"},
            "salary_max_lpa": {"type": ["number", "null"], "description": "Maximum annual pay in LPA, if stated"},
            "location_ok": {"type": "boolean",
                            "description": "Could someone based in the candidate's country/city realistically "
                                           "take this role (remote-eligible, or an accepted location)?"},
            "score": {"type": "integer", "description": "0-100 fit score for THIS candidate against THIS posting"},
            "reasons": {"type": "array", "items": {"type": "string"}, "description": "Short bullets: why it fits"},
            "red_flags": {"type": "array", "items": {"type": "string"},
                         "description": "Short bullets: concerns (seniority mismatch, vague, likely scam, etc.)"},
        },
        "required": ["work_mode", "score", "reasons", "red_flags", "location_ok"],
        "additionalProperties": False,
    },
}

INSTRUCTIONS = """\
You screen job postings for one candidate. You are given the candidate's profile and search criteria, then one
posting at a time. Call assess_job with your findings.
Be conservative: only report salary/experience numbers the posting actually states; leave them null if it
doesn't say. Score honestly: a generic or vague posting, a big seniority mismatch, or a likely scam should score
low; a strong match on skills, seniority and location should score high.
"""


def build_instructions(c: Criteria) -> str:
    return "\n".join([
        INSTRUCTIONS,
        "CANDIDATE PROFILE:\n" + json.dumps(config.PROFILE, indent=2, ensure_ascii=False),
        "SEARCH CRITERIA:\n" + c.model_dump_json(indent=2),
    ])


def evaluate(instructions: str, row: dict) -> dict:
    posting = {"title": row["title"], "company": row["company"], "location": row["location"],
               "description": (row.get("description") or "")[:6000]}
    resp = client.responses.create(
        model=config.JOBS_MODEL, instructions=instructions,
        input=[{"role": "user",
                "content": [{"type": "input_text", "text": json.dumps(posting, ensure_ascii=False)}]}],
        tools=[_TOOL], tool_choice="required")
    call = next(o for o in resp.output if o.type == "function_call")
    return json.loads(call.arguments or "{}")


def merge(job, ev: dict) -> dict:
    """Combine the LLM's extraction into the shape db.save_eval() expects."""
    work_mode = ev.get("work_mode")
    work_mode = work_mode if work_mode in ("remote", "hybrid", "onsite") else None
    location_ok = True if work_mode == "remote" else bool(ev.get("location_ok", True))
    return {
        "work_mode": work_mode, "exp_min": ev.get("exp_min"), "exp_max": ev.get("exp_max"),
        "salary_min_lpa": ev.get("salary_min_lpa"), "salary_max_lpa": ev.get("salary_max_lpa"),
        "location_ok": location_ok, "score": int(ev.get("score", 0)),
        "reasons": ev.get("reasons") or [], "red_flags": ev.get("red_flags") or [],
    }
