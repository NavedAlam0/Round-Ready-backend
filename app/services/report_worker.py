"""Process durable report jobs using short database transactions.

A lease reserves a job while the AI runs without an open database connection.
Every write checks the lease token, so an old worker cannot overwrite recovery
work. The AI response is checkpointed before the report is published atomically.
"""

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import logging
import uuid

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.models.interview import Interview
from app.models.report_job import ReportJob
from app.models.score import InterviewReport, InterviewScore
from app.services.report_evaluation import (
    InvalidEvaluationResult,
    InvalidReportInput,
    evaluate_snapshot,
    get_incomplete_reason,
    validate_evaluation,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedReportJob:
    job_id: str
    interview_id: str
    lease_token: str
    attempt_count: int
    input_snapshot: dict
    evaluation_result: dict | None
    attempts_exhausted: bool = False


class LeaseLostError(Exception):
    """This worker no longer owns the job and must discard its result."""


def _classify_failure(exc: Exception) -> tuple[bool, str, str]:
    """Return retryability and safe, persisted errors without provider payloads."""
    if isinstance(exc, InvalidReportInput):
        return False, "invalid_input", "The saved interview data cannot be evaluated."
    if isinstance(exc, InvalidEvaluationResult):
        return True, "invalid_evaluation", "The AI response was incomplete or invalid."
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)) or type(exc).__name__ in (
        "APIConnectionError", "APITimeoutError",
    ):
        return True, "provider_unavailable", "Report generation was interrupted."
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status in (408, 409, 429) or status >= 500:
            return True, "provider_unavailable", "The AI service is temporarily unavailable."
        if status in (401, 403):
            return False, "provider_configuration_error", "The AI service could not authorize report generation."
    if isinstance(exc, SQLAlchemyError):
        return True, "database_unavailable", "Report progress could not be saved."
    return False, "evaluation_failed", "Report generation failed and needs investigation."


class ReportWorker:
    def __init__(
        self,
        session_factory,
        *,
        evaluator: Callable[[dict], Awaitable[dict]] | None = None,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 15,
        max_attempts: int = 3,
        retry_base_seconds: float = 30,
        evaluation_timeout_seconds: float = 300,
    ):
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease")
        if max_attempts < 1 or retry_base_seconds < 0 or evaluation_timeout_seconds <= 0:
            raise ValueError("Invalid retry count, retry delay, or evaluation timeout")
        self.sessions = session_factory
        self.evaluator = evaluator if evaluator is not None else evaluate_snapshot
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.evaluation_timeout_seconds = evaluation_timeout_seconds

    async def claim_next_job(self) -> ClaimedReportJob | None:
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(ReportJob).where(or_(
                    and_(
                        ReportJob.status.in_(("queued", "retrying")),
                        ReportJob.next_attempt_at <= func.clock_timestamp(),
                    ),
                    and_(
                        ReportJob.status == "generating",
                        or_(
                            ReportJob.lease_expires_at <= func.clock_timestamp(),
                            ReportJob.lease_expires_at.is_(None),
                        ),
                    ),
                )).order_by(
                    ReportJob.next_attempt_at, ReportJob.created_at, ReportJob.id,
                ).with_for_update(skip_locked=True).limit(1)
            )
            if job is None:
                return None
            now = await db.scalar(select(func.clock_timestamp()))
            exhausted = job.attempt_count >= self.max_attempts
            # A saved AI response may still be published after the last allowed
            # attempt crashed. Without a checkpoint, never make another AI call.
            if not exhausted:
                job.attempt_count += 1
            job.status = "generating"
            job.stage = "saving_report" if job.evaluation_result is not None else "preparing"
            job.lease_token = str(uuid.uuid4())
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.updated_at = now
            claim = ClaimedReportJob(
                job.id, job.interview_id, job.lease_token, job.attempt_count,
                deepcopy(job.input_snapshot), deepcopy(job.evaluation_result), exhausted,
            )
        return claim

    def _owned(self, claim: ClaimedReportJob):
        return (
            ReportJob.id == claim.job_id,
            ReportJob.status == "generating",
            ReportJob.lease_token == claim.lease_token,
            ReportJob.lease_expires_at > func.clock_timestamp(),
        )

    async def _locked_job(self, db, claim: ClaimedReportJob) -> ReportJob:
        job = await db.scalar(select(ReportJob).where(*self._owned(claim)).with_for_update())
        if job is None:
            raise LeaseLostError()
        return job

    async def renew_lease(self, claim: ClaimedReportJob) -> bool:
        async with self.sessions.begin() as db:
            job_id = await db.scalar(
                update(ReportJob).where(*self._owned(claim)).values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=self.lease_seconds),
                    updated_at=func.clock_timestamp(),
                ).returning(ReportJob.id)
            )
        return job_id is not None

    async def _heartbeat(self, claim: ClaimedReportJob) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.renew_lease(claim):
                raise LeaseLostError()

    async def run_once(self) -> bool:
        """Process at most one job; False means there is no eligible work."""
        claim = await self.claim_next_job()
        if claim is None:
            return False
        await self.process_job(claim)
        return True

    async def process_job(self, claim: ClaimedReportJob) -> None:
        work = asyncio.create_task(self._process_claim(claim))
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            done, _ = await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            # A successful final commit releases the lease; prefer that success
            # if the heartbeat observes the released lease at the same instant.
            if work in done:
                await work
            else:
                await heartbeat
        except LeaseLostError:
            logger.info("Report job %s lease lost; discarding stale work", claim.job_id)
        except Exception as exc:
            # Stop the other task before attempting to persist the failure.
            for task in (work, heartbeat):
                task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
            retryable, code, message = _classify_failure(exc)
            logger.warning("Report job %s encountered %s (%s)", claim.job_id, code, type(exc).__name__)
            try:
                await self._record_failure(claim, retryable, code, message)
            except LeaseLostError:
                pass
            except Exception as save_error:
                # The durable lease will expire and allow another attempt even
                # if the database is unavailable while recording the failure.
                logger.error("Could not record failure for job %s (%s)", claim.job_id, type(save_error).__name__)
        finally:
            # Cancellation/shutdown leaves the durable claim for recovery after
            # its lease expires. Never swallow CancelledError or orphan the AI.
            for task in (work, heartbeat):
                task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def _process_claim(self, claim: ClaimedReportJob) -> None:
        async with self.sessions.begin() as db:
            job = await self._locked_job(db, claim)
            if await self._finish_existing_report(db, job):
                return

        reason = get_incomplete_reason(claim.input_snapshot)
        if reason is not None:
            async with self.sessions.begin() as db:
                job = await self._locked_job(db, claim)
                if not await self._finish_existing_report(db, job):
                    await self._finish(db, job, "incomplete", "insufficient_input", reason)
            return

        if claim.evaluation_result is not None:
            evaluation = validate_evaluation(claim.evaluation_result)
        elif claim.attempts_exhausted:
            await self._record_failure(
                claim, False, "attempts_exhausted", "Report generation could not finish within the retry limit.",
            )
            return
        else:
            async with self.sessions.begin() as db:
                job = await self._locked_job(db, claim)
                job.stage = "evaluating"
                job.updated_at = func.clock_timestamp()
            evaluation = validate_evaluation(await asyncio.wait_for(
                self.evaluator(deepcopy(claim.input_snapshot)), timeout=self.evaluation_timeout_seconds,
            ))
            async with self.sessions.begin() as db:
                job = await self._locked_job(db, claim)
                job.evaluation_result = evaluation
                job.stage = "saving_report"
                job.updated_at = func.clock_timestamp()

        async with self.sessions.begin() as db:
            job = await self._locked_job(db, claim)
            fields = {key: value for key, value in evaluation.items() if key != "category_scores"}
            # Legacy code can still create a report while this worker is running.
            # Only the transaction that inserts the report may add its scores.
            inserted_id = await db.scalar(
                insert(InterviewReport).values(interview_id=claim.interview_id, **fields)
                .on_conflict_do_nothing(index_elements=[InterviewReport.interview_id])
                .returning(InterviewReport.id)
            )
            if inserted_id is not None:
                db.add_all([
                    InterviewScore(interview_id=claim.interview_id, **category)
                    for category in evaluation["category_scores"]
                ])
            await self._finish(db, job, "ready")

    async def _finish_existing_report(self, db, job) -> bool:
        existing = await db.scalar(select(InterviewReport.id).where(
            InterviewReport.interview_id == job.interview_id,
        ))
        if existing is None:
            return False
        await self._finish(db, job, "ready")
        return True

    async def _finish(self, db, job, status: str, code=None, message=None) -> None:
        job.status = status
        job.stage = "complete" if status == "ready" else status
        job.last_error_code = code
        job.last_error_message = message
        job.completed_at = func.clock_timestamp()
        job.updated_at = func.clock_timestamp()
        job.lease_token = None
        job.lease_expires_at = None
        # A report failure does not undo a completed interview. Its failure is
        # recorded on the job; the interview and transcript remain completed.
        if status in ("ready", "incomplete"):
            await db.execute(update(Interview).where(Interview.id == job.interview_id).values(
                status="evaluated" if status == "ready" else status,
                incomplete_reason=message if status == "incomplete" else None,
            ))

    async def _record_failure(self, claim, retryable: bool, code: str, message: str) -> None:
        async with self.sessions.begin() as db:
            job = await self._locked_job(db, claim)
            # The legacy evaluator may have published while our AI request was
            # running. A completed report takes precedence over this failure.
            if await self._finish_existing_report(db, job):
                return
            if not retryable or job.attempt_count >= self.max_attempts:
                await self._finish(db, job, "failed", code, message)
                return
            delay = min(self.retry_base_seconds * (2 ** (job.attempt_count - 1)), 300)
            job.status = "retrying"
            job.last_error_code = code
            job.last_error_message = message
            job.next_attempt_at = func.clock_timestamp() + timedelta(seconds=delay)
            job.updated_at = func.clock_timestamp()
            job.completed_at = None
            job.lease_token = None
            job.lease_expires_at = None
