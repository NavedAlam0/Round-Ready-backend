"""Atomically collect final inputs and enqueue report work.

All entry points lock the interview row, so callback/final-save ordering does
not matter. Callers commit the transaction; no AI or network calls happen here.
"""

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.interview import Interview
from app.models.report_job import ReportJob
from app.models.score import InterviewReport
from app.services.report_jobs import (
    InterviewNotFoundError,
    ReportJobNotReadyError,
    create_report_job,
)


class DomainFinalSubmissionConflict(ValueError):
    """A different final submission has already been accepted."""


async def _lock_interview(db, interview_id: str, user_id: str | None = None) -> Interview:
    query = select(Interview).where(Interview.id == interview_id)
    if user_id is not None:
        query = query.where(Interview.user_id == user_id)
    interview = await db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if interview is None:
        raise InterviewNotFoundError("Interview not found")
    return interview


async def _existing(db, interview):
    job = await db.scalar(select(ReportJob).where(ReportJob.interview_id == interview.id))
    report_id = await db.scalar(select(InterviewReport.id).where(InterviewReport.interview_id == interview.id))
    return job, report_id


async def _queue_if_ready(db, interview):
    job, report_id = await _existing(db, interview)
    if job is not None or report_id is not None:
        return job
    if interview.status != "completed" or not interview.transcript:
        return None
    if interview.interview_mode == "coding" and interview.code_finalized_at is None:
        return None
    return await create_report_job(db, interview.id, user_id=interview.user_id)


async def save_code_submission(
    db, interview_id: str, *, user_id: str, code: str,
    language: str | None = None, is_final: bool = False,
) -> ReportJob | None:
    interview = await _lock_interview(db, interview_id, user_id)
    if interview.interview_mode != "coding":
        raise ReportJobNotReadyError("This interview does not accept coding submissions")
    job, report_id = await _existing(db, interview)
    if interview.code_finalized_at is not None or job is not None or report_id is not None:
        if is_final and (
            code != (interview.final_code or "")
            or (language is not None and language != interview.coding_language)
        ):
            raise DomainFinalSubmissionConflict("A different final code submission was already accepted")
        # A delayed periodic backup must never overwrite the final submission.
        return job
    interview.final_code = code
    if language is not None:
        interview.coding_language = language
    if is_final:
        interview.code_finalized_at = datetime.now(timezone.utc)
    await db.flush()
    return await _queue_if_ready(db, interview)


async def finalize_saved_code(db, interview_id: str, *, user_id: str) -> ReportJob | None:
    """Explicitly submit the last server backup when the browser lost its draft."""
    interview = await _lock_interview(db, interview_id, user_id)
    job, report_id = await _existing(db, interview)
    if job is not None or report_id is not None:
        return job
    if interview.status != "completed":
        raise ReportJobNotReadyError("Finish the interview before submitting its saved code")
    return await save_code_submission(
        db, interview_id, user_id=user_id, code=interview.final_code or "",
        language=interview.coding_language, is_final=True,
    )


async def complete_from_callback(
    db, interview_id: str, *, transcript: list, status: str | None = None,
) -> ReportJob | None:
    interview = await _lock_interview(db, interview_id)
    job, report_id = await _existing(db, interview)
    if job is not None or report_id is not None:
        return job
    # Keep the first nonempty transcript even if duplicate callbacks arrive
    # while a coding interview is still waiting for its final submission.
    if not interview.transcript:
        interview.transcript = deepcopy(transcript)
        if transcript:
            interview.status = "completed"
            interview.incomplete_reason = None
        else:
            interview.status = "failed" if status == "failed" else "incomplete"
            interview.incomplete_reason = "No interview transcript was received."
    await db.flush()
    return await _queue_if_ready(db, interview)


async def end_completed_interview(db, interview_id: str, *, user_id: str) -> Interview:
    interview = await _lock_interview(db, interview_id, user_id)
    job, report_id = await _existing(db, interview)
    if job is None and report_id is None and interview.status not in ("evaluated", "incomplete", "failed"):
        interview.status = "completed"
        await db.flush()
        await _queue_if_ready(db, interview)
    return interview


async def request_report(db, interview_id: str, *, user_id: str) -> ReportJob | None:
    interview = await _lock_interview(db, interview_id, user_id)
    job, report_id = await _existing(db, interview)
    if job is not None or report_id is not None:
        return job
    if interview.status != "completed":
        raise ReportJobNotReadyError("The interview is not completed")
    if not interview.transcript:
        raise ReportJobNotReadyError("The interview transcript has not been received")
    if interview.interview_mode == "coding" and interview.code_finalized_at is None:
        raise ReportJobNotReadyError("The final code submission has not been received")
    return await _queue_if_ready(db, interview)
