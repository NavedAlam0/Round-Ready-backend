"""Report-job integration checks against a disposable local PostgreSQL server.

Run from backend: venv/bin/python -m unittest tests.test_report_jobs -v
Requires initdb and pg_ctl on PATH. The suite starts its own database using a
private Unix socket with TCP disabled; it never loads application settings.
"""

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from tests.postgres_support import PostgresTestCase


class ReportJobTests(PostgresTestCase):
    async def test_job_persists_and_freezes_inputs(self):
        interview_id = await self.make_interview()
        async with self.sessions.begin() as db:
            job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
            job_id = job.id
            self.assertEqual((job.status, job.attempt_count), ("queued", 0))
            interview = await db.get(self.models.Interview, interview_id)
            interview.transcript[0]["text"] = "Edited later"
            self.assertEqual(job.input_snapshot["transcript"][0]["text"], "This is my original answer.")
            interview.transcript = [{"role": "user", "text": "Replacement answer"}]
        async with self.sessions() as db:
            saved = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            self.assertEqual(saved.id, job_id)
            self.assertEqual(saved.input_snapshot["schema_version"], 1)
            self.assertEqual(saved.input_snapshot["transcript"][0]["text"], "This is my original answer.")

    async def test_lookup_and_creation_enforce_ownership(self):
        interview_id = await self.make_interview()
        async with self.sessions.begin() as db:
            await self.jobs.create_report_job(db, interview_id, user_id="owner")
        async with self.sessions() as db:
            self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="other"))
            self.assertIsNone(await self.jobs.get_report_job(db, "missing", user_id="owner"))
            for requested_id, user_id in [(interview_id, "other"), ("missing", "owner")]:
                with self.assertRaises(self.jobs.InterviewNotFoundError):
                    await self.jobs.create_report_job(db, requested_id, user_id=user_id)

    async def test_creation_reads_inputs_updated_by_another_session(self):
        interview_id = await self.make_interview()
        async with self.sessions() as db:
            cached = await db.get(self.models.Interview, interview_id)
            self.assertEqual(cached.transcript[0]["text"], "This is my original answer.")
            async with self.sessions.begin() as other_db:
                interview = await other_db.get(self.models.Interview, interview_id)
                interview.transcript = [{"role": "user", "text": "Latest committed answer"}]
            job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
            self.assertEqual(job.input_snapshot["transcript"][0]["text"], "Latest committed answer")
            await db.commit()

    async def test_lookup_reads_progress_updated_by_another_session(self):
        interview_id = await self.make_interview()
        async with self.sessions.begin() as db:
            await self.jobs.create_report_job(db, interview_id, user_id="owner")
        async with self.sessions() as db:
            cached = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            self.assertEqual(cached.status, "queued")
            async with self.sessions.begin() as other_db:
                job = await self.jobs.get_report_job(other_db, interview_id, user_id="owner")
                job.status = "generating"
                job.attempt_count = 1
            refreshed = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            self.assertEqual((refreshed.status, refreshed.attempt_count), ("generating", 1))

    async def test_repeated_creation_preserves_progress_and_original_inputs(self):
        for status in ("generating", "retrying", "failed", "ready", "incomplete"):
            with self.subTest(status=status):
                interview_id = await self.make_interview()
                async with self.sessions.begin() as db:
                    job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
                    job_id = job.id
                    job.status = status
                    job.stage = "saving_report"
                    job.attempt_count = 2
                    job.evaluation_result = {"overall_score": 7}
                    job.last_error_code = "temporary_failure"
                    interview = await db.get(self.models.Interview, interview_id)
                    interview.status = "in_progress"
                    interview.transcript = None
                async with self.sessions.begin() as db:
                    repeated = await self.jobs.create_report_job(db, interview_id, user_id="owner")
                    self.assertEqual(repeated.id, job_id)
                    self.assertEqual((repeated.status, repeated.attempt_count), (status, 2))
                    self.assertEqual(repeated.stage, "saving_report")
                    self.assertEqual(repeated.evaluation_result, {"overall_score": 7})
                    self.assertEqual(repeated.last_error_code, "temporary_failure")
                    self.assertTrue(repeated.input_snapshot["transcript"])

    async def test_unfinished_or_missing_inputs_are_rejected(self):
        for overrides in (
            {"status": "in_progress"}, {"transcript": None}, {"interview_plan": None},
            {"interview_mode": "coding", "coding_problem": None}, {"interview_mode": "unknown"},
        ):
            with self.subTest(overrides=overrides):
                interview_id = await self.make_interview(**overrides)
                async with self.sessions() as db:
                    with self.assertRaises(self.jobs.ReportJobNotReadyError):
                        await self.jobs.create_report_job(db, interview_id, user_id="owner")
                    self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="owner"))

    async def test_empty_coding_submission_is_queued_for_worker_to_assess(self):
        interview_id = await self.make_interview(
            interview_mode="coding", coding_language="python", final_code=None,
            coding_problem={"title": "Test problem", "starter_code": {"python": "pass"}},
        )
        async with self.sessions.begin() as db:
            job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
            self.assertEqual(job.input_snapshot["interview_mode"], "coding")
            self.assertEqual(job.input_snapshot["coding_language"], "python")
            self.assertEqual(job.input_snapshot["coding_problem"]["title"], "Test problem")
            self.assertIsNone(job.input_snapshot["final_code"])

    async def test_existing_legacy_report_is_not_queued_again(self):
        interview_id = await self.make_interview(status="evaluated")
        async with self.sessions.begin() as db:
            db.add(self.models.InterviewReport(
                interview_id=interview_id, overall_score=7, strengths=[], weaknesses=[],
                detailed_feedback="Saved report", recommendations=[],
            ))
        async with self.sessions() as db:
            with self.assertRaises(self.jobs.ReportAlreadyExistsError):
                await self.jobs.create_report_job(db, interview_id, user_id="owner")
            self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="owner"))

    async def test_caller_rollback_removes_job_and_input_changes(self):
        interview_id = await self.make_interview()
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            interview.transcript = [{"role": "user", "text": "Uncommitted answer"}]
            await self.jobs.create_report_job(db, interview_id, user_id="owner")
            await db.rollback()
        async with self.sessions() as db:
            self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="owner"))
            interview = await db.get(self.models.Interview, interview_id)
            self.assertEqual(interview.transcript[0]["text"], "This is my original answer.")

    async def test_concurrent_creators_receive_the_same_job(self):
        interview_id = await self.make_interview()
        original_lookup = self.jobs.get_report_job
        barrier = asyncio.Barrier(2)

        async def lookup_after_both_read(db, requested_id, *, user_id):
            job = await original_lookup(db, requested_id, user_id=user_id)
            if job is None:
                await barrier.wait()
            return job

        async def create():
            async with self.sessions.begin() as db:
                job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
                return job.id

        with patch.object(self.jobs, "get_report_job", side_effect=lookup_after_both_read):
            ids = await asyncio.wait_for(asyncio.gather(create(), create()), timeout=10)
        self.assertEqual(ids[0], ids[1])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(self.models.ReportJob)), 1)

    async def test_competing_creator_can_continue_after_first_rolls_back(self):
        interview_id = await self.make_interview()
        original_lookup = self.jobs.get_report_job
        second_checked = asyncio.Event()

        async def observe_second_lookup(db, requested_id, *, user_id):
            job = await original_lookup(db, requested_id, user_id=user_id)
            second_checked.set()
            return job

        async def create_second():
            async with self.sessions.begin() as db:
                job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
                return job.id

        async with self.sessions() as first_db:
            first_job = await self.jobs.create_report_job(first_db, interview_id, user_id="owner")
            abandoned_id = first_job.id
            with patch.object(self.jobs, "get_report_job", side_effect=observe_second_lookup):
                second = asyncio.create_task(create_second())
                try:
                    await asyncio.wait_for(second_checked.wait(), timeout=10)
                    await first_db.rollback()
                    second_id = await asyncio.wait_for(second, timeout=10)
                finally:
                    if not second.done():
                        second.cancel()
                        await asyncio.gather(second, return_exceptions=True)
        self.assertNotEqual(abandoned_id, second_id)
        async with self.sessions() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            self.assertEqual(job.id, second_id)
            self.assertEqual(await db.scalar(select(func.count()).select_from(self.models.ReportJob)), 1)


if __name__ == "__main__":
    unittest.main()
