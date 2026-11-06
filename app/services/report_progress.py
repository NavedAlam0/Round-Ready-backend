"""Owner-scoped, public report progress shared by HTTP and SSE."""

from sqlalchemy import select

from app.models.interview import Interview
from app.models.report_job import ReportJob
from app.models.score import InterviewReport
from app.services.report_jobs import InterviewNotFoundError


async def get_report_progress(db, interview_id: str, *, user_id: str) -> dict:
    interview = await db.scalar(select(Interview).where(
        Interview.id == interview_id, Interview.user_id == user_id,
    ).execution_options(populate_existing=True))
    if interview is None:
        raise InterviewNotFoundError("Interview not found")
    # Read report before job so a published report always wins over old states.
    report_id = await db.scalar(select(InterviewReport.id).where(InterviewReport.interview_id == interview_id))
    if report_id is not None:
        return {"status": "ready", "stage": "complete", "message": "Your report is ready."}
    job = await db.scalar(select(ReportJob).where(ReportJob.interview_id == interview_id)
                          .execution_options(populate_existing=True))
    if job is not None:
        messages = {
            "queued": "Your report is waiting to be generated.",
            "generating": "Your interview is being evaluated.",
            "retrying": "Report generation was interrupted. We will try again automatically.",
            "failed": "We could not generate your report. Your interview has been saved.",
            "incomplete": "There was not enough interview material to generate a report.",
            "ready": "Your report is ready.",
        }
        return {
            "status": job.status, "stage": job.stage,
            "attempt_count": job.attempt_count,
            "message": job.last_error_message if job.status in ("failed", "incomplete") else messages[job.status],
            "error_code": job.last_error_code,
            "next_attempt_at": job.next_attempt_at.isoformat() if job.status == "retrying" else None,
        }
    if interview.status in ("failed", "incomplete"):
        return {
            "status": interview.status,
            "message": interview.incomplete_reason or "The interview could not be completed.",
        }
    if interview.status not in ("completed", "evaluated"):
        return {"status": "not_started", "message": "Finish the interview to generate its report."}
    if not interview.transcript:
        return {"status": "awaiting_transcript", "message": "Waiting for the interview transcript."}
    if interview.interview_mode == "coding" and interview.code_finalized_at is None:
        return {"status": "awaiting_submission", "message": "Waiting for your final code submission."}
    return {"status": "not_started", "message": "Your interview is saved and ready to be queued for evaluation."}
