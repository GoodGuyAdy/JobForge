"""Small persistence layer: reusable answers (qa_bank.json) and per-run logs."""
import json
import re
import time
from pathlib import Path

import config


def _norm(question: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", question.casefold())).strip()


def _read() -> dict:
    try:
        return json.loads(config.QA_BANK_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_answer(question: str, answer: str) -> None:
    bank = _read()
    bank[_norm(question)] = {"question": question.strip(), "answer": answer.strip()}
    tmp = config.QA_BANK_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.QA_BANK_PATH)


def saved_answers() -> list:
    return list(_read().values())


def new_run_dir(url: str) -> Path:
    run_dir = config.RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"url": url}), encoding="utf-8")
    return run_dir


def log_step(run_dir: Path, record: dict) -> None:
    with (run_dir / "steps.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_result(run_dir: Path, result: dict) -> None:
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")