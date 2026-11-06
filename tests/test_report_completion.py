"""Completion handoff integration tests using isolated local PostgreSQL."""

import asyncio
from copy import deepcopy
import unittest

from sqlalchemy import func, select

from tests.postgres_support import PostgresTestCase


TRANSCRIPT = [
    {"role": "user", "text": "I designed the database schema for our application."},
    {"role": "user", "text": "I used an index to improve the query performance."},
    {"role": "user", "text": "I tested the change using realistic production data."},
]
FINAL_CODE = "def solve(values):\n    total = 0\n    for value in values:\n        total += value\n    return total"


class ReportCompletionTests(PostgresTestCase):
    extra_modules = {"completion": "app.services.report_completion"}

    async def make_coding(self, **overrides):
        inputs = {
            "status": "in_progress", "transcript": None,
            "interview_mode": "coding", "coding_language": "python",
            "coding_problem": {"title": "Sum the values", "starter_code": {"python": "def solve(values):\n    pass"}},
            "final_code": "def solve(values):\n    pass",
        }
        inputs.update(overrides)
        return await self.make_interview(**inputs)

    async def state(self, interview_id):
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            return interview, job

    async def save_final(self, db, interview_id, **overrides):
        data = {"user_id": "owner", "code": FINAL_CODE, "language": "python", "is_final": True}
        data.update(overrides)
        return await self.completion.save_code_submission(db, interview_id, **data)

    async def callback(self, db, interview_id, **overrides):
        data = {"transcript": deepcopy(TRANSCRIPT), "status": "completed"}
        data.update(overrides)
        return await self.completion.complete_from_callback(db, interview_id, **data)

    async def test_voice_callback_saves_transcript_and_job_together(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)
        async with self.sessions.begin() as db:
            job = await self.callback(db, interview_id)
            self.assertIsNotNone(job)
            job_id = job.id
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "completed")
        self.assertEqual(interview.transcript, TRANSCRIPT)
        self.assertEqual((job.id, job.status, job.attempt_count), (job_id, "queued", 0))
        self.assertEqual(job.input_snapshot["transcript"], TRANSCRIPT)

    async def test_callback_rollback_leaves_no_transcript_or_job(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)
        async with self.sessions() as db:
            await self.callback(db, interview_id)
            await db.rollback()
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "in_progress")
        self.assertIsNone(interview.transcript)
        self.assertIsNone(job)

    async def test_coding_waits_for_final_submission_after_transcript(self):
        interview_id = await self.make_coding()
        async with self.sessions.begin() as db:
            self.assertIsNone(await self.callback(db, interview_id))
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "completed")
        self.assertEqual(interview.transcript, TRANSCRIPT)
        self.assertIsNone(interview.code_finalized_at)
        self.assertIsNone(job)
        async with self.sessions.begin() as db:
            self.assertIsNotNone(await self.save_final(db, interview_id))
        interview, job = await self.state(interview_id)
        self.assertIsNotNone(interview.code_finalized_at)
        self.assertEqual(job.input_snapshot["final_code"], FINAL_CODE)
        self.assertEqual(job.input_snapshot["transcript"], TRANSCRIPT)

    async def test_coding_waits_for_transcript_after_final_submission(self):
        interview_id = await self.make_coding()
        async with self.sessions.begin() as db:
            self.assertIsNone(await self.save_final(db, interview_id))
        interview, job = await self.state(interview_id)
        self.assertIsNotNone(interview.code_finalized_at)
        self.assertEqual(interview.final_code, FINAL_CODE)
        self.assertIsNone(job)
        async with self.sessions.begin() as db:
            self.assertIsNotNone(await self.callback(db, interview_id))
        _, job = await self.state(interview_id)
        self.assertEqual(job.input_snapshot["final_code"], FINAL_CODE)
        self.assertEqual(job.input_snapshot["transcript"], TRANSCRIPT)

    async def test_concurrent_callback_and_final_submission_create_one_complete_snapshot(self):
        interview_id = await self.make_coding()
        barrier = asyncio.Barrier(2)

        async def complete():
            async with self.sessions.begin() as db:
                await barrier.wait()
                return await self.callback(db, interview_id)

        async def submit():
            async with self.sessions.begin() as db:
                await barrier.wait()
                return await self.save_final(db, interview_id)

        results = await asyncio.wait_for(asyncio.gather(complete(), submit()), timeout=10)
        self.assertTrue(any(result is not None for result in results))
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "completed")
        self.assertEqual(job.input_snapshot["final_code"], FINAL_CODE)
        self.assertEqual(job.input_snapshot["transcript"], TRANSCRIPT)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(self.models.ReportJob)), 1)

    async def test_backup_is_not_final_and_late_backup_cannot_overwrite_submission(self):
        interview_id = await self.make_coding()
        async with self.sessions.begin() as db:
            await self.callback(db, interview_id)
            self.assertIsNone(await self.completion.save_code_submission(
                db, interview_id, user_id="owner", code="backup", language="python",
            ))
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.final_code, "backup")
        self.assertIsNone(interview.code_finalized_at)
        self.assertIsNone(job)
        async with self.sessions.begin() as db:
            job = await self.save_final(db, interview_id)
            job_id = job.id
        async with self.sessions.begin() as db:
            await self.completion.save_code_submission(
                db, interview_id, user_id="owner", code="stale backup", language="javascript",
            )
        interview, job = await self.state(interview_id)
        self.assertEqual((interview.final_code, interview.coding_language), (FINAL_CODE, "python"))
        self.assertEqual((job.id, job.input_snapshot["final_code"]), (job_id, FINAL_CODE))

    async def test_repeated_identical_final_is_idempotent_and_different_final_conflicts(self):
        interview_id = await self.make_coding(transcript=deepcopy(TRANSCRIPT), status="completed")
        async with self.sessions.begin() as db:
            job = await self.save_final(db, interview_id)
            job_id = job.id
        before, _ = await self.state(interview_id)
        async with self.sessions.begin() as db:
            repeated = await self.save_final(db, interview_id)
            self.assertEqual(repeated.id, job_id)
        for override in ({"code": "changed final"}, {"language": "javascript"}):
            with self.subTest(override=override):
                async with self.sessions() as db:
                    with self.assertRaises(self.completion.DomainFinalSubmissionConflict):
                        await self.save_final(db, interview_id, **override)
                    await db.rollback()
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.code_finalized_at, before.code_finalized_at)
        self.assertEqual((interview.final_code, interview.coding_language), (FINAL_CODE, "python"))
        self.assertEqual(job.id, job_id)

    async def test_final_submission_rollback_preserves_unfinalized_inputs(self):
        interview_id = await self.make_coding(transcript=deepcopy(TRANSCRIPT), status="completed")
        before, _ = await self.state(interview_id)
        async with self.sessions() as db:
            await self.save_final(db, interview_id)
            await db.rollback()
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.final_code, before.final_code)
        self.assertIsNone(interview.code_finalized_at)
        self.assertIsNone(job)

    async def test_empty_final_submission_is_queued_for_worker_to_assess(self):
        interview_id = await self.make_coding(status="completed", transcript=deepcopy(TRANSCRIPT))
        async with self.sessions.begin() as db:
            job = await self.save_final(db, interview_id, code="")
            self.assertEqual(job.input_snapshot["final_code"], "")
        interview, job = await self.state(interview_id)
        self.assertIsNotNone(interview.code_finalized_at)
        self.assertEqual(job.status, "queued")

    async def test_explicit_backup_finalization_requires_completed_owned_interview(self):
        interview_id = await self.make_coding(final_code="last saved backup")
        async with self.sessions() as db:
            with self.assertRaises(self.jobs.ReportJobNotReadyError):
                await self.completion.finalize_saved_code(db, interview_id, user_id="owner")
        async with self.sessions.begin() as db:
            await self.callback(db, interview_id)
        async with self.sessions() as db:
            with self.assertRaises(self.jobs.InterviewNotFoundError):
                await self.completion.finalize_saved_code(db, interview_id, user_id="other")
        async with self.sessions.begin() as db:
            job = await self.completion.finalize_saved_code(db, interview_id, user_id="owner")
            job_id = job.id
            self.assertEqual(job.input_snapshot["final_code"], "last saved backup")
        async with self.sessions.begin() as db:
            repeated = await self.completion.finalize_saved_code(db, interview_id, user_id="owner")
            self.assertEqual(repeated.id, job_id)
        interview, job = await self.state(interview_id)
        self.assertIsNotNone(interview.code_finalized_at)
        self.assertEqual(job.input_snapshot["final_code"], "last saved backup")

    async def test_duplicate_callback_preserves_transcript_while_waiting_for_code(self):
        interview_id = await self.make_coding()
        async with self.sessions.begin() as db:
            await self.callback(db, interview_id)
        async with self.sessions.begin() as db:
            await self.callback(db, interview_id, transcript=[{"role": "user", "text": "A later conflicting callback."}])
            job = await self.save_final(db, interview_id)
            self.assertEqual(job.input_snapshot["transcript"], TRANSCRIPT)
        interview, _ = await self.state(interview_id)
        self.assertEqual(interview.transcript, TRANSCRIPT)

    async def test_duplicate_completion_does_not_reset_existing_job(self):
        for status, interview_status in (("generating", "completed"), ("retrying", "completed"),
                                          ("failed", "completed"), ("incomplete", "incomplete"),
                                          ("ready", "evaluated")):
            with self.subTest(status=status):
                interview_id = await self.make_interview(status="in_progress", transcript=None)
                async with self.sessions.begin() as db:
                    job = await self.callback(db, interview_id)
                    job.status = status
                    job.attempt_count = 2
                    job.stage = "saving_report"
                    job.evaluation_result = {"overall_score": 8}
                    interview = await db.get(self.models.Interview, interview_id)
                    interview.status = interview_status
                    job_id = job.id
                async with self.sessions.begin() as db:
                    repeated = await self.callback(db, interview_id, transcript=[], status="failed")
                    self.assertEqual(repeated.id, job_id)
                    await self.completion.end_completed_interview(db, interview_id, user_id="owner")
                    requested = await self.completion.request_report(db, interview_id, user_id="owner")
                    self.assertEqual(requested.id, job_id)
                interview, job = await self.state(interview_id)
                self.assertEqual(interview.status, interview_status)
                self.assertEqual(interview.transcript, TRANSCRIPT)
                self.assertEqual((job.status, job.attempt_count, job.stage), (status, 2, "saving_report"))
                self.assertEqual(job.evaluation_result, {"overall_score": 8})

    async def test_empty_transcript_records_terminal_outcome_without_job(self):
        for callback_status, expected in (("completed", "incomplete"), ("failed", "failed")):
            with self.subTest(callback_status=callback_status):
                interview_id = await self.make_interview(status="in_progress", transcript=None)
                async with self.sessions.begin() as db:
                    self.assertIsNone(await self.callback(db, interview_id, transcript=[], status=callback_status))
                interview, job = await self.state(interview_id)
                self.assertEqual(interview.status, expected)
                self.assertEqual(interview.transcript, [])
                self.assertIsNone(job)
                async with self.sessions.begin() as db:
                    await self.completion.end_completed_interview(db, interview_id, user_id="owner")
                interview, _ = await self.state(interview_id)
                self.assertEqual(interview.status, expected)

    async def test_owner_checks_cover_submission_end_and_manual_request(self):
        interview_id = await self.make_coding()
        for requested_id, user_id in ((interview_id, "other"), ("missing", "owner")):
            for operation in ("submit", "end", "request"):
                with self.subTest(requested_id=requested_id, user_id=user_id, operation=operation):
                    async with self.sessions() as db:
                        with self.assertRaises(self.jobs.InterviewNotFoundError):
                            if operation == "submit":
                                await self.save_final(db, requested_id, user_id=user_id)
                            elif operation == "end":
                                await self.completion.end_completed_interview(db, requested_id, user_id=user_id)
                            else:
                                await self.completion.request_report(db, requested_id, user_id=user_id)
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "in_progress")
        self.assertIsNone(interview.code_finalized_at)
        self.assertIsNone(job)

    async def test_manual_request_waits_for_completed_inputs_and_final_code(self):
        cases = [
            {"status": "in_progress", "transcript": None},
            {"status": "completed", "transcript": None},
            {"status": "completed", "transcript": deepcopy(TRANSCRIPT)},
        ]
        for values in cases:
            with self.subTest(values=values):
                interview_id = await self.make_coding(**values)
                async with self.sessions() as db:
                    with self.assertRaises(self.jobs.ReportJobNotReadyError):
                        await self.completion.request_report(db, interview_id, user_id="owner")
                _, job = await self.state(interview_id)
                self.assertIsNone(job)
        interview_id = await self.make_interview(transcript=deepcopy(TRANSCRIPT))
        async with self.sessions.begin() as db:
            job = await self.completion.request_report(db, interview_id, user_id="owner")
            self.assertEqual((job.status, job.attempt_count), ("queued", 0))

    async def test_existing_legacy_report_is_preserved_by_callback_and_end(self):
        interview_id = await self.make_interview(status="evaluated", transcript=deepcopy(TRANSCRIPT))
        async with self.sessions.begin() as db:
            report = self.models.InterviewReport(
                interview_id=interview_id, overall_score=7, strengths=[], weaknesses=[],
                detailed_feedback="Existing report", recommendations=[],
            )
            db.add(report)
            await db.flush()
            report_id = report.id
        async with self.sessions.begin() as db:
            self.assertIsNone(await self.callback(db, interview_id, transcript=[], status="failed"))
            await self.completion.end_completed_interview(db, interview_id, user_id="owner")
            self.assertIsNone(await self.completion.request_report(db, interview_id, user_id="owner"))
        interview, job = await self.state(interview_id)
        self.assertEqual(interview.status, "evaluated")
        self.assertEqual(interview.transcript, TRANSCRIPT)
        self.assertIsNone(job)
        async with self.sessions() as db:
            report = await db.scalar(select(self.models.InterviewReport).where(
                self.models.InterviewReport.interview_id == interview_id,
            ))
            self.assertEqual((report.id, report.overall_score), (report_id, 7))


if __name__ == "__main__":
    unittest.main()
