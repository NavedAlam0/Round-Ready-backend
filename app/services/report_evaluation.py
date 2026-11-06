"""Validate saved report inputs and model output without loading app settings.

The worker checks eligibility before making an AI request. Evaluators are
imported only when a request is actually needed, so these checks also work in
offline tests without database credentials or an AI provider configuration.
"""

import json
import math


class InvalidReportInput(ValueError):
    """The saved job input cannot be evaluated safely."""


class InvalidEvaluationResult(ValueError):
    """The model response is not a usable report; evaluation may be retried."""


def _validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise InvalidReportInput("Report input must be an object")
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise InvalidReportInput("Unsupported report input schema version")
    mode = snapshot.get("interview_mode")
    if mode not in ("voice", "coding"):
        raise InvalidReportInput("Unsupported interview mode")

    transcript = snapshot.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        raise InvalidReportInput("Report input requires a nonempty transcript")
    for entry in transcript:
        if not isinstance(entry, dict):
            raise InvalidReportInput("Transcript entries must be objects")
        role, text = entry.get("role"), entry.get("text")
        if not isinstance(role, str) or not role.strip() or not isinstance(text, str):
            raise InvalidReportInput("Transcript entries require a role and text string")

    for field in ("interview_plan", "coding_problem"):
        if not isinstance(snapshot.get(field), dict):
            raise InvalidReportInput(f"Report input {field} must be an object")
    required_field = "interview_plan" if mode == "voice" else "coding_problem"
    if not snapshot[required_field]:
        raise InvalidReportInput(f"Report input requires {required_field}")
    for field in ("final_code", "coding_language"):
        if snapshot.get(field) is not None and not isinstance(snapshot[field], str):
            raise InvalidReportInput(f"Report input {field} must be a string or null")

    starter_code = snapshot["coding_problem"].get("starter_code")
    if starter_code is not None:
        if not isinstance(starter_code, dict) or any(
            not isinstance(language, str) or not isinstance(code, str)
            for language, code in starter_code.items()
        ):
            raise InvalidReportInput("Coding starter_code must map languages to strings")


def get_incomplete_reason(snapshot: dict) -> str | None:
    """Return why the interview lacks enough material, preserving current rules.

    Invalid saved data raises InvalidReportInput. A valid but insufficient
    interview returns a reason so the worker can persist an incomplete outcome
    without making a paid evaluation request.
    """
    _validate_snapshot(snapshot)
    if snapshot["interview_mode"] == "voice":
        answers = [
            entry
            for entry in snapshot["transcript"]
            if entry["role"] == "user" and len(entry["text"].strip().split()) >= 5
        ]
        if len(answers) < 3:
            return "At least 3 candidate answers of 5 or more words are required."
        return None

    final_code = snapshot.get("final_code")
    if not final_code or not final_code.strip():
        return "No code was submitted."
    starter_code = ""
    language = snapshot.get("coding_language")
    if language:
        starter_code = (snapshot["coding_problem"].get("starter_code") or {}).get(
            language, ""
        )
    if starter_code and final_code.strip() == starter_code.strip():
        return "Only the starter code was submitted — no changes were made."
    starter_lines = {line.strip() for line in starter_code.splitlines() if line.strip()}
    new_lines = [
        line
        for line in final_code.splitlines()
        if line.strip() and line.strip() not in starter_lines
    ]
    if len(new_lines) < 3:
        return (
            f"Only {len(new_lines)} new line(s) of code written. "
            "Write at least 3 lines beyond the starter code."
        )
    return None


def _score(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 10
        or not math.isfinite(value)
    ):
        raise InvalidEvaluationResult(f"{field} must be a finite number from 0 to 10")
    return float(value)


def validate_evaluation(result: dict) -> dict:
    """Return a detached, JSON-safe report containing only persisted fields.

    Empty category scores are rejected, including the legacy evaluators' error
    fallback. A provider/parse failure must be retried, not saved as a zero-score
    assessment of the candidate.
    """
    if not isinstance(result, dict):
        raise InvalidEvaluationResult("Evaluation must be an object")
    normalized = {"overall_score": _score(result.get("overall_score"), "overall_score")}
    categories = result.get("category_scores")
    if not isinstance(categories, list) or not categories:
        raise InvalidEvaluationResult("Evaluation requires nonempty category_scores")
    normalized["category_scores"] = []
    for category in categories:
        if not isinstance(category, dict):
            raise InvalidEvaluationResult("Category scores must be objects")
        name = category.get("category")
        if not isinstance(name, str) or not name.strip():
            raise InvalidEvaluationResult("Category scores require a nonempty category")
        feedback = category.get("feedback")
        if feedback is not None and not isinstance(feedback, str):
            raise InvalidEvaluationResult("Category feedback must be a string or null")
        normalized["category_scores"].append({
            "category": name.strip(),
            "score": _score(category.get("score"), "category score"),
            "feedback": feedback.strip() if feedback is not None else None,
        })

    for field in ("strengths", "weaknesses", "recommendations"):
        items = result.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise InvalidEvaluationResult(f"{field} must be a list of strings")
        normalized[field] = [item.strip() for item in items]
    feedback = result.get("detailed_feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise InvalidEvaluationResult("Evaluation requires nonempty detailed_feedback")
    normalized["detailed_feedback"] = feedback.strip()

    for field in ("question_answers", "skill_assessment"):
        items = result.get(field, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise InvalidEvaluationResult(f"{field} must be a list of objects")
        normalized[field] = items
    for field in ("confidence_analysis", "code_review"):
        value = result.get(field)
        if value is not None and not isinstance(value, dict):
            raise InvalidEvaluationResult(f"{field} must be an object or null")
        normalized[field] = value

    # Nested AI fields are retained, but cannot contain NaN/Infinity or values
    # that a database JSON column cannot store. Round-tripping also prevents a
    # later mutation of the provider response from changing this checkpoint.
    try:
        return json.loads(json.dumps(normalized, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise InvalidEvaluationResult("Evaluation contains invalid JSON data") from exc


async def evaluate_snapshot(snapshot: dict) -> dict:
    """Evaluate valid saved inputs; callers check eligibility before calling."""
    _validate_snapshot(snapshot)
    if snapshot["interview_mode"] == "coding":
        from app.services.coding_evaluator import evaluate_coding_interview

        result = await evaluate_coding_interview(
            transcript=snapshot["transcript"],
            coding_problem=snapshot["coding_problem"],
            final_code=snapshot.get("final_code"),
            coding_language=snapshot.get("coding_language"),
        )
    else:
        from app.services.evaluator import evaluate_interview

        result = await evaluate_interview(
            transcript=snapshot["transcript"],
            interview_plan=snapshot["interview_plan"],
        )
    return validate_evaluation(result)
