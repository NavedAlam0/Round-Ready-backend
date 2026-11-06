"""Worker integration tests using disposable PostgreSQL and fake AI responses.

Run from backend: venv/bin/python -m unittest tests.test_report_worker -v
Application configuration and external services are never loaded.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select, text

from tests.postgres_support import PostgresTestCase


TRANSCRIPT = [
    {"role": "user", "text": "I designed the database schema for our application."},
    {"role": "user", "text": "I used an index to improve the query performance."},
    {"role": "user", "text": "I tested the change using realistic production data."},
]
EVALUATION = {
    "overall_score": 8,
    "category_scores": [
        {"category": "Technical", "score": 8, "feedback": "Clear technical explanation."},
        {"category": "Communication", "score": 7, "feedback": "Explained the tradeoffs."},
    ],
    "strengths": ["Explains decisions clearly"],
    "weaknesses": ["Could discuss more edge cases"],
    "detailed_feedback": "The answers demonstrated sound understanding of the problem.",
    "recommendations": ["Practice explaining failure scenarios"],
    "question_answers": [{"question": "How did you test it?", "answer": "With realistic data", "score": 8}],
    "confidence_analysis": {"overall_confidence": "high"},
    "skill_assessment": [{"skill": "Databases", "score": 8}],
    "code_review": {"summary": "The implementation handles each input value."},
}
CODING_INPUTS = {
    "interview_mode": "coding",
    "coding_language": "python",
    "coding_problem": {"title": "Sum the values", "starter_code": {"python": "def solve(values):\n    pass"}},
    "final_code": "def solve(values):\n    total = 0\n    for value in values:\n        total += value\n    return total",
}


class ReportWorkerTests(PostgresTestCase):
    extra_modules = {"worker_module": "app.services.report_worker"}

    def worker(self, evaluator=None, **options):
        if evaluator is None:
            evaluator = AsyncMock(return_value=deepcopy(EVALUATION))
        worker = self.worker_module.ReportWorker(self.sessions, evaluator=evaluator, **options)
        return worker, evaluator

    async def queue_job(self, **interview_overrides):
        inputs = {"transcript": deepcopy(TRANSCRIPT)}
        inputs.update(deepcopy(interview_overrides))
        interview_id = await self.make_interview(**inputs)
        async with self.sessions.begin() as db:
            job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
            return interview_id, job.id

    async def load_state(self, interview_id, job_id):
        async with self.sessions() as db:
            job = await db.get(self.models.ReportJob, job_id)
            interview = await db.get(self.models.Interview, interview_id)
            report = (await db.execute(select(self.models.InterviewReport).where(
                self.models.InterviewReport.interview_id == interview_id,
            ))).scalar_one_or_none()
            scores = list((await db.execute(select(self.models.InterviewScore).where(
                self.models.InterviewScore.interview_id == interview_id,
            ).order_by(self.models.InterviewScore.category))).scalars())
            return job, interview, report, scores

    async def expire_lease(self, job_id, **overrides):
        async with self.sessions.begin() as db:
            job = await db.get(self.models.ReportJob, job_id)
            job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            for name, value in overrides.items():
                setattr(job, name, value)

    async def test_no_work_and_concurrent_claims_are_exclusive(self):
        first, first_evaluator = self.worker()
        second, _ = self.worker()
        self.assertFalse(await first.run_once())
        interview_id, job_id = await self.queue_job()
        claims = await asyncio.wait_for(asyncio.gather(
            first.claim_next_job(), second.claim_next_job(),
        ), timeout=10)
        claimed = [claim for claim in claims if claim is not None]
        self.assertEqual(len(claimed), 1)
        claim = claimed[0]
        self.assertEqual((claim.job_id, claim.interview_id, claim.attempt_count), (job_id, interview_id, 1))
        self.assertTrue(claim.lease_token)
        self.assertIsNone(await second.claim_next_job())
        job, _, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "generating")
        self.assertEqual(job.lease_token, claim.lease_token)
        self.assertGreater(job.lease_expires_at, datetime.now(timezone.utc))
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        first_evaluator.assert_not_awaited()

    async def test_voice_and_coding_publish_complete_report_once(self):
        for overrides in ({}, CODING_INPUTS):
            with self.subTest(mode=overrides.get("interview_mode", "voice")):
                interview_id, job_id = await self.queue_job(**overrides)
                worker, evaluator = self.worker()
                self.assertTrue(await worker.run_once())
                job, interview, report, scores = await self.load_state(interview_id, job_id)
                self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
                self.assertIsNotNone(job.completed_at)
                self.assertIsNone(job.lease_token)
                self.assertIsNone(job.lease_expires_at)
                self.assertEqual(report.overall_score, 8)
                self.assertEqual(report.strengths, EVALUATION["strengths"])
                self.assertEqual(report.detailed_feedback, EVALUATION["detailed_feedback"])
                for field in ("question_answers", "confidence_analysis", "skill_assessment", "code_review"):
                    self.assertEqual(getattr(report, field), EVALUATION[field])
                self.assertEqual([(score.category, score.score) for score in scores], [
                    ("Communication", 7), ("Technical", 8),
                ])
                evaluator.assert_awaited_once()
                self.assertEqual(evaluator.await_args.args[0]["interview_mode"], overrides.get("interview_mode", "voice"))
                self.assertFalse(await worker.run_once())
                async with self.sessions() as db:
                    count = await db.scalar(select(func.count()).select_from(self.models.InterviewReport).where(
                        self.models.InterviewReport.interview_id == interview_id,
                    ))
                self.assertEqual(count, 1)
                evaluator.assert_awaited_once()

    async def test_worker_uses_frozen_job_inputs_after_interview_changes(self):
        interview_id, job_id = await self.queue_job(**CODING_INPUTS)
        async with self.sessions.begin() as db:
            interview = await db.get(self.models.Interview, interview_id)
            interview.transcript = [{"role": "user", "text": "Changed after queueing"}]
            interview.final_code = "replacement code"
        worker, evaluator = self.worker()
        await worker.run_once()
        snapshot = evaluator.await_args.args[0]
        self.assertEqual(snapshot["transcript"], TRANSCRIPT)
        self.assertEqual(snapshot["final_code"], CODING_INPUTS["final_code"])
        job, _, report, _ = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "ready")
        self.assertIsNotNone(report)

    async def test_insufficient_voice_or_coding_input_is_incomplete_without_ai(self):
        cases = [
            {"transcript": [TRANSCRIPT[0]]},
            dict(CODING_INPUTS, final_code=None),
        ]
        for inputs in cases:
            with self.subTest(mode=inputs.get("interview_mode", "voice")):
                interview_id, job_id = await self.queue_job(**inputs)
                worker, evaluator = self.worker()
                self.assertTrue(await worker.run_once())
                job, interview, report, scores = await self.load_state(interview_id, job_id)
                self.assertEqual((job.status, interview.status), ("incomplete", "incomplete"))
                self.assertTrue(interview.incomplete_reason)
                self.assertIsNotNone(job.completed_at)
                self.assertIsNone(job.lease_token)
                self.assertIsNone(report)
                self.assertEqual(scores, [])
                self.assertFalse(await worker.run_once())
                evaluator.assert_not_awaited()

    async def test_transient_errors_retry_then_stop_at_attempt_limit(self):
        interview_id, job_id = await self.queue_job()
        evaluator = AsyncMock(side_effect=TimeoutError("Simulated provider timeout"))
        worker, _ = self.worker(evaluator, max_attempts=2, retry_base_seconds=0)
        self.assertTrue(await worker.run_once())
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, job.attempt_count), ("retrying", 1))
        self.assertTrue(job.last_error_code)
        self.assertTrue(job.last_error_message)
        self.assertIsNone(job.lease_token)
        self.assertIsNone(job.completed_at)
        self.assertEqual(interview.status, "completed")
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        self.assertTrue(await worker.run_once())
        job, _, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, job.attempt_count), ("failed", 2))
        self.assertIsNotNone(job.completed_at)
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        self.assertFalse(await worker.run_once())
        self.assertEqual(evaluator.await_count, 2)

    async def test_retry_delay_prevents_immediate_reclaim(self):
        interview_id, job_id = await self.queue_job()
        worker, evaluator = self.worker(AsyncMock(side_effect=ConnectionError("Provider unavailable")), retry_base_seconds=30)
        await worker.run_once()
        job, _, _, _ = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "retrying")
        self.assertGreater(job.next_attempt_at, datetime.now(timezone.utc))
        self.assertFalse(await worker.run_once())
        evaluator.assert_awaited_once()

    async def test_provider_authorization_error_fails_without_retry(self):
        interview_id, job_id = await self.queue_job()
        response = httpx.Response(401, request=httpx.Request("POST", "https://ai.example.invalid/evaluate"))
        error = httpx.HTTPStatusError("Private provider response must not be persisted", request=response.request, response=response)
        worker, evaluator = self.worker(AsyncMock(side_effect=error), retry_base_seconds=0)
        self.assertTrue(await worker.run_once())
        job, _, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, job.attempt_count), ("failed", 1))
        self.assertEqual(job.last_error_code, "provider_configuration_error")
        self.assertNotIn("Private provider response", job.last_error_message)
        self.assertIsNotNone(job.completed_at)
        self.assertIsNone(job.lease_token)
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        self.assertFalse(await worker.run_once())
        evaluator.assert_awaited_once()

    async def test_evaluation_deadline_stops_a_hung_provider(self):
        interview_id, job_id = await self.queue_job()
        cancelled = asyncio.Event()

        async def hung_evaluation(snapshot):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        worker, _ = self.worker(hung_evaluation, max_attempts=1, evaluation_timeout_seconds=0.05)
        self.assertTrue(await asyncio.wait_for(worker.run_once(), timeout=5))
        self.assertTrue(cancelled.is_set())
        job, _, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.last_error_code)
        self.assertIsNone(report)
        self.assertEqual(scores, [])

    async def test_invalid_saved_input_fails_without_ai(self):
        interview_id, job_id = await self.queue_job()
        async with self.sessions.begin() as db:
            job = await db.get(self.models.ReportJob, job_id)
            job.input_snapshot = dict(job.input_snapshot, schema_version=999)
        worker, evaluator = self.worker()
        await worker.run_once()
        job, _, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.last_error_code)
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        evaluator.assert_not_awaited()

    async def test_malformed_ai_fallback_is_retried_without_zero_score_report(self):
        interview_id, job_id = await self.queue_job()
        fallback = dict(EVALUATION, overall_score=0, category_scores=[])
        worker, _ = self.worker(AsyncMock(return_value=fallback), retry_base_seconds=0)
        await worker.run_once()
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "retrying")
        self.assertIsNone(job.evaluation_result)
        self.assertEqual(interview.status, "completed")
        self.assertIsNone(report)
        self.assertEqual(scores, [])

    async def test_expired_lease_reuses_checkpoint_even_at_attempt_limit(self):
        interview_id, job_id = await self.queue_job()
        original, _ = self.worker()
        old_claim = await original.claim_next_job()
        await self.expire_lease(job_id, evaluation_result=deepcopy(EVALUATION), attempt_count=3)
        recovered, evaluator = self.worker(max_attempts=3)
        new_claim = await recovered.claim_next_job()
        self.assertIsNotNone(new_claim)
        self.assertNotEqual(new_claim.lease_token, old_claim.lease_token)
        self.assertIsNotNone(new_claim.evaluation_result)
        await recovered.process_job(new_claim)
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
        self.assertEqual(report.overall_score, 8)
        self.assertEqual(len(scores), 2)
        evaluator.assert_not_awaited()

    async def test_expired_final_attempt_without_checkpoint_fails_without_ai(self):
        interview_id, job_id = await self.queue_job()
        original, _ = self.worker(max_attempts=3)
        await original.claim_next_job()
        await self.expire_lease(job_id, attempt_count=3)
        recovered, evaluator = self.worker(max_attempts=3)
        claim = await recovered.claim_next_job()
        self.assertIsNotNone(claim)
        self.assertTrue(claim.attempts_exhausted)
        self.assertEqual(claim.attempt_count, 3)
        self.assertIsNone(claim.evaluation_result)
        await recovered.process_job(claim)
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, job.attempt_count), ("failed", 3))
        self.assertEqual(job.last_error_code, "attempts_exhausted")
        self.assertIsNotNone(job.completed_at)
        self.assertIsNone(job.lease_token)
        self.assertEqual(interview.status, "completed")
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        self.assertFalse(await recovered.run_once())
        evaluator.assert_not_awaited()

    async def test_stale_worker_cannot_renew_or_publish_after_reclaim(self):
        interview_id, job_id = await self.queue_job()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_evaluation(snapshot):
            started.set()
            await release.wait()
            return dict(EVALUATION, overall_score=2)

        stale_worker, _ = self.worker(slow_evaluation)
        stale_claim = await stale_worker.claim_next_job()
        stale_task = asyncio.create_task(stale_worker.process_job(stale_claim))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await self.expire_lease(job_id)
            current_worker, current_evaluator = self.worker()
            current_claim = await current_worker.claim_next_job()
            self.assertIsNotNone(current_claim)
            self.assertNotEqual(current_claim.lease_token, stale_claim.lease_token)
            self.assertFalse(await stale_worker.renew_lease(stale_claim))
            release.set()
            await asyncio.wait_for(stale_task, timeout=5)
            job, interview, report, scores = await self.load_state(interview_id, job_id)
            self.assertEqual(job.lease_token, current_claim.lease_token)
            self.assertEqual((job.status, interview.status), ("generating", "completed"))
            self.assertIsNone(job.evaluation_result)
            self.assertIsNone(report)
            self.assertEqual(scores, [])
            await current_worker.process_job(current_claim)
            job, _, report, _ = await self.load_state(interview_id, job_id)
            self.assertEqual(job.status, "ready")
            self.assertEqual(report.overall_score, 8)
            current_evaluator.assert_awaited_once()
        finally:
            release.set()
            if not stale_task.done():
                stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)

    async def test_heartbeat_keeps_long_evaluation_claim_alive(self):
        interview_id, job_id = await self.queue_job()
        release, renewed = asyncio.Event(), asyncio.Event()

        async def slow_evaluation(snapshot):
            await release.wait()
            return deepcopy(EVALUATION)

        worker, _ = self.worker(slow_evaluation, lease_seconds=1, heartbeat_seconds=0.05)
        claim = await worker.claim_next_job()
        before, _, _, _ = await self.load_state(interview_id, job_id)
        original_renew = worker.renew_lease

        async def observe_renewal(current_claim):
            result = await original_renew(current_claim)
            if result:
                renewed.set()
            return result

        with patch.object(worker, "renew_lease", side_effect=observe_renewal):
            task = asyncio.create_task(worker.process_job(claim))
            try:
                await asyncio.wait_for(renewed.wait(), timeout=5)
                after, _, _, _ = await self.load_state(interview_id, job_id)
                self.assertGreater(after.lease_expires_at, before.lease_expires_at)
                competitor, _ = self.worker()
                self.assertIsNone(await competitor.claim_next_job())
                release.set()
                await asyncio.wait_for(task, timeout=5)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_evaluation_can_be_recovered(self):
        interview_id, job_id = await self.queue_job()
        started = asyncio.Event()

        async def interrupted_evaluation(snapshot):
            started.set()
            await asyncio.Event().wait()

        worker, _ = self.worker(interrupted_evaluation)
        task = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertIn(job.status, ("queued", "generating", "retrying"))
        self.assertEqual(interview.status, "completed")
        self.assertIsNone(report)
        self.assertEqual(scores, [])
        await self.expire_lease(job_id, next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        recovered, evaluator = self.worker()
        self.assertTrue(await recovered.run_once())
        job, _, report, _ = await self.load_state(interview_id, job_id)
        self.assertEqual(job.status, "ready")
        self.assertIsNotNone(report)
        evaluator.assert_awaited_once()

    async def test_existing_report_short_circuits_ai_and_preserves_report(self):
        interview_id, job_id = await self.queue_job(transcript=[TRANSCRIPT[0]])
        async with self.sessions.begin() as db:
            report = self.models.InterviewReport(
                interview_id=interview_id, overall_score=6, strengths=[], weaknesses=[],
                detailed_feedback="Previously generated report", recommendations=[],
            )
            db.add(report)
            await db.flush()
            report_id = report.id
        worker, evaluator = self.worker()
        await worker.run_once()
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
        self.assertEqual((report.id, report.overall_score), (report_id, 6))
        self.assertEqual(scores, [])
        evaluator.assert_not_awaited()

    async def test_legacy_report_inserted_during_evaluation_keeps_its_scores(self):
        interview_id, job_id = await self.queue_job()
        inserted = {}

        async def concurrent_legacy_evaluation(snapshot):
            # The worker has already checked for a report. Simulate the legacy
            # route completing its own transaction while the AI request runs.
            async with self.sessions.begin() as db:
                report = self.models.InterviewReport(
                    interview_id=interview_id, overall_score=6, strengths=[], weaknesses=[],
                    detailed_feedback="Report completed by legacy route", recommendations=[],
                )
                score = self.models.InterviewScore(
                    interview_id=interview_id, category="Legacy category", score=6,
                    feedback="Existing evaluation feedback",
                )
                db.add_all([report, score])
                await db.flush()
                inserted.update(report_id=report.id, score_id=score.id)
            return deepcopy(EVALUATION)

        evaluator = AsyncMock(side_effect=concurrent_legacy_evaluation)
        worker, _ = self.worker(evaluator)
        self.assertTrue(await worker.run_once())
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
        self.assertEqual((report.id, report.overall_score), (inserted["report_id"], 6))
        self.assertEqual([(score.id, score.category, score.score) for score in scores], [
            (inserted["score_id"], "Legacy category", 6),
        ])
        self.assertFalse(await worker.run_once())
        evaluator.assert_awaited_once()

    async def test_legacy_report_wins_when_worker_evaluation_fails(self):
        response = httpx.Response(401, request=httpx.Request("POST", "https://ai.example.invalid/evaluate"))
        failures = [
            httpx.HTTPStatusError("Provider rejected request", request=response.request, response=response),
            TimeoutError("Provider request timed out"),
        ]
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                interview_id, job_id = await self.queue_job()
                inserted = {}

                async def failed_concurrent_evaluation(snapshot):
                    async with self.sessions.begin() as db:
                        report = self.models.InterviewReport(
                            interview_id=interview_id, overall_score=6, strengths=[], weaknesses=[],
                            detailed_feedback="Report completed by legacy route", recommendations=[],
                        )
                        score = self.models.InterviewScore(
                            interview_id=interview_id, category="Legacy category", score=6,
                            feedback="Existing evaluation feedback",
                        )
                        db.add_all([report, score])
                        await db.flush()
                        inserted.update(report_id=report.id, score_id=score.id)
                    raise failure

                evaluator = AsyncMock(side_effect=failed_concurrent_evaluation)
                worker, _ = self.worker(evaluator, retry_base_seconds=0)
                self.assertTrue(await worker.run_once())
                job, interview, report, scores = await self.load_state(interview_id, job_id)
                self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
                self.assertIsNone(job.last_error_code)
                self.assertIsNone(job.last_error_message)
                self.assertIsNone(job.lease_token)
                self.assertIsNotNone(job.completed_at)
                self.assertEqual((report.id, report.overall_score), (inserted["report_id"], 6))
                self.assertEqual([(score.id, score.category, score.score) for score in scores], [
                    (inserted["score_id"], "Legacy category", 6),
                ])
                self.assertFalse(await worker.run_once())
                evaluator.assert_awaited_once()

    async def test_report_save_failure_rolls_back_scores_and_reuses_checkpoint(self):
        interview_id, job_id = await self.queue_job()
        async with self.engine.begin() as connection:
            await connection.execute(text("""
                CREATE FUNCTION reject_test_report() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Simulated serialization failure' USING ERRCODE = '40001';
                END $$
            """))
            await connection.execute(text("""
                CREATE TRIGGER reject_test_report_publish BEFORE UPDATE ON report_jobs
                FOR EACH ROW WHEN (NEW.status = 'ready') EXECUTE FUNCTION reject_test_report()
            """))
        try:
            worker, evaluator = self.worker(retry_base_seconds=0)
            await worker.run_once()
            job, interview, report, scores = await self.load_state(interview_id, job_id)
            self.assertEqual(job.status, "retrying")
            self.assertIsNotNone(job.evaluation_result)
            self.assertEqual(interview.status, "completed")
            self.assertIsNone(report)
            self.assertEqual(scores, [])
        finally:
            async with self.engine.begin() as connection:
                await connection.execute(text("DROP TRIGGER reject_test_report_publish ON report_jobs"))
                await connection.execute(text("DROP FUNCTION reject_test_report()"))
        self.assertTrue(await worker.run_once())
        job, interview, report, scores = await self.load_state(interview_id, job_id)
        self.assertEqual((job.status, interview.status), ("ready", "evaluated"))
        self.assertEqual(report.overall_score, 8)
        self.assertEqual(len(scores), 2)
        evaluator.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
