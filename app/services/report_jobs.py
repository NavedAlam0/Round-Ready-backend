"""Create and look up durable report jobs without starting evaluation.

Callers supply a verified user ID and own the database transaction: these
helpers never commit it. Completion handlers save final interview inputs and
their report job together in the same transaction.
"""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interview import Interview
from app.models.report_job import ReportJob
from app.models.score import InterviewReport


class InterviewNotFoundError(LookupError):
    """The interview does not exist or is not owned by the supplied user."""


class ReportJobNotReadyError(ValueError):
    """The interview is not ready to be queued for evaluation."""


class ReportAlreadyExistsError(ValueError):
    """A legacy interview already has a report without a report-job record."""


async def get_report_job(
    db: AsyncSession, interview_id: str, *, user_id: str
) -> ReportJob | None:
    """Return the owner's job, or None if it is absent or inaccessible."""
    result = await db.execute(
        select(ReportJob)
        .join(Interview, ReportJob.interview_id == Interview.id)
        .where(Interview.id == interview_id, Interview.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def create_report_job(
    db: AsyncSession, interview_id: str, *, user_id: str
) -> ReportJob:
    """Queue a completed interview once, preserving any existing job.

    The caller must finish collecting inputs, including the final code save,
    before calling this function. Detailed answer/code sufficiency checks
    belong to the worker so it can persist an 'incomplete' outcome.

    Repeated calls never reset status, retry counters, or saved inputs. The
    database resolves concurrent inserts using the unique interview ID.
    """
    existing = await get_report_job(db, interview_id, user_id=user_id)
    if existing is not None:
        return existing

    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id, Interview.user_id == user_id
        )
        .execution_options(populate_existing=True)
    )
    interview = result.scalar_one_or_none()
    if interview is None:
        raise InterviewNotFoundError("Interview not found")

    report_id = await db.scalar(
        select(InterviewReport.id).where(InterviewReport.interview_id == interview_id)
    )
    if report_id is not None:
        raise ReportAlreadyExistsError("A report already exists for this interview")

    if interview.status != "completed":
        raise ReportJobNotReadyError("The interview must be completed before creating a report job")
    if not interview.transcript:
        raise ReportJobNotReadyError("The interview transcript has not been received")
    if interview.interview_mode == "voice":
        if not interview.interview_plan:
            raise ReportJobNotReadyError("The interview plan is missing")
    elif interview.interview_mode == "coding":
        if not interview.coding_problem:
            raise ReportJobNotReadyError("The coding problem is missing")
    else:
        raise ReportJobNotReadyError("Unsupported interview mode")

    snapshot = deepcopy({
        "schema_version": 1,
        "interview_mode": interview.interview_mode,
        "transcript": interview.transcript,
        "interview_plan": interview.interview_plan or {},
        "coding_problem": interview.coding_problem or {},
        "final_code": interview.final_code,
        "coding_language": interview.coding_language,
    })
    result = await db.execute(
        insert(ReportJob)
        .values(interview_id=interview_id, input_snapshot=snapshot)
        .on_conflict_do_nothing(index_elements=[ReportJob.interview_id])
        .returning(ReportJob)
    )
    job = result.scalar_one_or_none()
    if job is not None:
        return job

    # Another transaction queued this interview first. Read its job without
    # overwriting the snapshot or resetting any progress it may have made.
    existing = await get_report_job(db, interview_id, user_id=user_id)
    if existing is None:
        raise RuntimeError("Report job was removed during creation; retry the operation")
    return existing
