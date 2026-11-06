"""HTTP report handoff and access checks without Firebase or provider calls."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx

from tests.postgres_support import PostgresTestCase
from tests.test_report_completion import FINAL_CODE, TRANSCRIPT


class NotificationStub:
    @contextmanager
    def watch(self, interview_id):
        yield asyncio.Event()


class ReportRouteTests(PostgresTestCase):
    extra_modules = {
        "completion": "app.services.report_completion",
        "notifications": "app.services.report_notifications",
        "worker_module": "app.services.report_worker",
        "routes": "app.routers.interview",
    }

    @classmethod
    def prepare_extra_imports(cls, database):
        async def unconfigured_database():
            raise AssertionError("Route tests must override database dependencies")
            yield

        async def unconfigured_user():
            raise AssertionError("Route tests must override the authentication dependency")

        async def unexpected_external_call(*args, **kwargs):
            raise AssertionError("An enqueue/report HTTP request must not call external services")

        database.get_db = unconfigured_database
        database.async_session = None
        config = types.ModuleType("app.config")
        config._offline_test_stub = True
        config.settings = types.SimpleNamespace(daily_api_key="offline-bot-secret")
        sys.modules["app.config"] = config
        auth = types.ModuleType("app.routers.auth")
        auth.get_current_user = unconfigured_user
        sys.modules["app.routers.auth"] = auth
        for name, attributes in {
            "question_planner": ("generate_interview_plan",),
            "coding_planner": ("generate_coding_problem",),
            "daily_helpers": ("create_daily_room", "get_daily_token"),
            "evaluator": ("evaluate_interview",),
            "coding_evaluator": ("evaluate_coding_interview",),
        }.items():
            module = types.ModuleType(f"app.services.{name}")
            for attribute in attributes:
                setattr(module, attribute, unexpected_external_call)
            sys.modules[module.__name__] = module

    async def asyncSetUp(self):
        await super().asyncSetUp()
        app = FastAPI()
        app.include_router(self.routes.router)
        app.state.report_notifications = NotificationStub()

        async def get_db():
            async with self.sessions() as db:
                yield db

        async def get_user(request: Request):
            authorization = request.headers.get("Authorization", "")
            if authorization not in ("Bearer owner", "Bearer other"):
                raise HTTPException(status_code=401, detail="Unauthenticated")
            user_id = authorization.removeprefix("Bearer ")
            async with self.sessions() as db:
                return await db.get(self.models.User, user_id)

        app.dependency_overrides[self.routes.get_db] = get_db
        app.dependency_overrides[self.routes.get_current_user] = get_user
        self.session_patch = patch.object(self.routes, "async_session", self.sessions)
        self.stop_patch = patch.object(self.routes, "_stop_voice_agent", new=AsyncMock())
        self.session_patch.start()
        self.stop_voice = self.stop_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test")
        self.owner_headers = {"Authorization": "Bearer owner"}
        self.callback_headers = {"X-Bot-Secret": "offline-bot-secret"}

    async def asyncTearDown(self):
        try:
            await self.client.aclose()
        finally:
            self.stop_patch.stop()
            self.session_patch.stop()
            await super().asyncTearDown()

    async def queue_voice(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)
        response = await self.client.post(
            f"/api/interview/{interview_id}/bot-callback", headers=self.callback_headers,
            json={"transcript": deepcopy(TRANSCRIPT), "status": "completed"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return interview_id

    async def test_callback_secret_is_required_and_empty_config_does_not_authorize(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)
        for headers in ({}, {"X-Bot-Secret": "wrong-secret"}):
            with self.subTest(headers=headers):
                response = await self.client.post(
                    f"/api/interview/{interview_id}/bot-callback", headers=headers,
                    json={"transcript": deepcopy(TRANSCRIPT), "status": "completed"},
                )
                self.assertEqual(response.status_code, 401)
        with patch.object(self.routes.settings, "daily_api_key", ""):
            response = await self.client.post(
                f"/api/interview/{interview_id}/bot-callback",
                json={"transcript": deepcopy(TRANSCRIPT), "status": "completed"},
            )
            self.assertEqual(response.status_code, 401)
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            self.assertIsNone(interview.transcript)
            self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="owner"))

    async def test_callback_validates_transcript_before_saving(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)
        for transcript in ({"not": "a list"}, [{"role": "user", "text": ["not text"]}]):
            with self.subTest(transcript=transcript):
                response = await self.client.post(
                    f"/api/interview/{interview_id}/bot-callback", headers=self.callback_headers,
                    json={"transcript": transcript, "status": "completed"},
                )
                self.assertEqual(response.status_code, 422)
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            self.assertIsNone(interview.transcript)

    async def test_callback_queues_without_evaluating_and_duplicate_preserves_job(self):
        interview_id = await self.queue_voice()
        async with self.sessions.begin() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            job_id = job.id
            job.status = "retrying"
            job.attempt_count = 2
        response = await self.client.post(
            f"/api/interview/{interview_id}/bot-callback", headers=self.callback_headers,
            json={"transcript": [], "status": "failed"},
        )
        self.assertEqual(response.status_code, 200)
        async with self.sessions() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            interview = await db.get(self.models.Interview, interview_id)
            self.assertEqual((job.id, job.status, job.attempt_count), (job_id, "retrying", 2))
            self.assertEqual(interview.transcript, TRANSCRIPT)

    async def test_manual_evaluation_returns_accepted_saved_job(self):
        interview_id = await self.make_interview(transcript=deepcopy(TRANSCRIPT))
        response = await self.client.post(
            f"/api/interview/{interview_id}/evaluate", headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        async with self.sessions() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            self.assertEqual((job.status, job.attempt_count), ("queued", 0))
        repeated = await self.client.post(
            f"/api/interview/{interview_id}/evaluate", headers=self.owner_headers,
        )
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json()["status"], "queued")

    async def test_code_sync_requires_final_marker_and_preserves_submitted_code(self):
        interview_id = await self.make_interview(
            interview_mode="coding", coding_language="python", transcript=deepcopy(TRANSCRIPT),
            coding_problem={"title": "Problem", "starter_code": {"python": "pass"}},
        )
        base = f"/api/interview/{interview_id}"
        response = await self.client.post(
            f"{base}/code-sync", headers=self.owner_headers,
            json={"code": "backup", "language": "python"},
        )
        self.assertEqual(response.status_code, 200)
        progress = await self.client.get(f"{base}/report", headers=self.owner_headers)
        self.assertEqual(progress.json()["status"], "awaiting_submission")
        response = await self.client.post(
            f"{base}/code-sync", headers=self.owner_headers,
            json={"code": FINAL_CODE, "language": "python", "is_final": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.post(
            f"{base}/code-sync", headers=self.owner_headers,
            json={"code": "late backup", "language": "javascript"},
        )
        self.assertEqual(response.status_code, 200)
        response = await self.client.post(
            f"{base}/code-sync", headers=self.owner_headers,
            json={"code": "changed final", "language": "python", "is_final": True},
        )
        self.assertEqual(response.status_code, 409)
        async with self.sessions() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            interview = await db.get(self.models.Interview, interview_id)
            self.assertEqual(interview.final_code, FINAL_CODE)
            self.assertEqual(job.input_snapshot["final_code"], FINAL_CODE)

    async def test_private_report_endpoints_reject_missing_identity_or_another_owner(self):
        interview_id = await self.queue_voice()
        paths = ("report", "report/events", "evaluate", "start", "end", "code-sync", "code-finalize")
        for suffix in paths:
            for headers, expected in (({}, 401), ({"Authorization": "Bearer other"}, 404)):
                with self.subTest(suffix=suffix, expected=expected):
                    if suffix in ("report", "report/events"):
                        response = await self.client.get(f"/api/interview/{interview_id}/{suffix}", headers=headers)
                    else:
                        response = await self.client.post(
                            f"/api/interview/{interview_id}/{suffix}", headers=headers,
                            json={"code": FINAL_CODE, "is_final": True} if suffix == "code-sync" else None,
                        )
                    self.assertEqual(response.status_code, expected, response.text)

    async def test_failed_job_is_reported_as_failed_instead_of_pending(self):
        interview_id = await self.queue_voice()
        async with self.sessions.begin() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            job.status = "failed"
            job.last_error_code = "provider_unavailable"
            job.last_error_message = "The AI service is temporarily unavailable."
            job.attempt_count = 3
        response = await self.client.get(
            f"/api/interview/{interview_id}/report", headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["status"], data["attempt_count"]), ("failed", 3))
        self.assertTrue(data["message"])
        self.assertNotIn("input_snapshot", data)
        self.assertNotIn("evaluation_result", data)

    async def test_report_committed_during_progress_read_returns_complete_ready_payload(self):
        interview_id = await self.queue_voice()
        original_progress = self.routes.get_report_progress

        async def publish_before_progress(db, requested_id, *, user_id):
            self.assertEqual((requested_id, user_id), (interview_id, "owner"))
            # This runs only after GET /report has already read no report.
            # Simulate the worker committing before the progress query runs.
            async with self.sessions.begin() as worker_db:
                worker_db.add(self.models.InterviewReport(
                    interview_id=interview_id, overall_score=7,
                    strengths=["Clear answers"], weaknesses=[],
                    detailed_feedback="Report committed during the request.",
                    recommendations=["Discuss more edge cases"],
                ))
                worker_db.add(self.models.InterviewScore(
                    interview_id=interview_id, category="Technical", score=7,
                    feedback="Explained the implementation.",
                ))
                job = await self.jobs.get_report_job(worker_db, interview_id, user_id="owner")
                job.status = "ready"
                interview = await worker_db.get(self.models.Interview, interview_id)
                interview.status = "evaluated"
            return await original_progress(db, requested_id, user_id=user_id)

        with patch.object(self.routes, "get_report_progress", side_effect=publish_before_progress) as progress:
            response = await self.client.get(
                f"/api/interview/{interview_id}/report", headers=self.owner_headers,
            )
        progress.assert_awaited_once()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["report"]["overall_score"], 7)
        self.assertEqual(payload["report"]["detailed_feedback"], "Report committed during the request.")
        self.assertEqual(payload["report"]["category_scores"][0]["category"], "Technical")
        self.assertEqual(payload["context"]["interview_id"], interview_id)

    async def test_terminal_sse_sends_current_status_on_every_reconnect(self):
        interview_id = await self.queue_voice()
        async with self.sessions.begin() as db:
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            job.status = "failed"
            job.last_error_code = "provider_unavailable"
            job.last_error_message = "Report generation could not finish."
        for _ in range(2):
            response = await asyncio.wait_for(self.client.get(
                f"/api/interview/{interview_id}/report/events", headers=self.owner_headers,
            ), timeout=5)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(response.headers.get("x-accel-buffering"), "no")
            self.assertIn("event: report_status", response.text)
            data_lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
            self.assertEqual(len(data_lines), 1)
            event = json.loads(data_lines[0])
            self.assertEqual(event["status"], "failed")
            self.assertNotIn("input_snapshot", event)
            self.assertNotIn("transcript", event)

    async def test_callback_worker_notification_stream_and_report_complete_together(self):
        async with self.engine.begin() as connection:
            await self.notifications.install_report_notifications(connection)
        hub = self.notifications.ReportNotificationHub(self.engine)
        await hub.start()
        stream = None
        try:
            interview_id = await self.queue_voice()
            request = types.SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
            stream = self.routes._report_event_stream(request, hub, interview_id, "owner")

            def event_payload(chunk):
                line = next(line for line in chunk.splitlines() if line.startswith("data: "))
                return json.loads(line.removeprefix("data: "))

            initial = event_payload(await asyncio.wait_for(anext(stream), timeout=5))
            self.assertEqual(initial["status"], "queued")
            evaluation = {
                "overall_score": 8,
                "category_scores": [{"category": "Technical", "score": 8, "feedback": "Clear explanation."}],
                "strengths": ["Explains decisions clearly"], "weaknesses": [],
                "detailed_feedback": "The candidate demonstrated sound technical understanding.",
                "recommendations": ["Practice explaining edge cases"],
            }
            evaluator = AsyncMock(return_value=evaluation)
            worker = self.worker_module.ReportWorker(self.sessions, evaluator=evaluator)
            self.assertTrue(await worker.run_once())
            # The real PostgreSQL trigger/LISTEN connection must wake the stream;
            # no timer, manual Event.set(), or report polling drives this read.
            ready = event_payload(await asyncio.wait_for(anext(stream), timeout=5))
            self.assertEqual(ready["status"], "ready")
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=5)
            response = await self.client.get(
                f"/api/interview/{interview_id}/report", headers=self.owner_headers,
            )
            self.assertEqual(response.status_code, 200)
            report = response.json()
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["report"]["overall_score"], 8)
            self.assertEqual(report["report"]["category_scores"][0]["category"], "Technical")
            self.assertEqual(report["context"]["interview_id"], interview_id)
            evaluator.assert_awaited_once()
            self.assertEqual(evaluator.await_args.args[0]["transcript"], TRANSCRIPT)
        finally:
            if stream is not None:
                await stream.aclose()
            await hub.close()

    async def test_end_commits_before_stopping_voice_and_preserves_ready_status(self):
        interview_id = await self.make_interview(status="in_progress", transcript=None)

        async def stopped(interview):
            async with self.sessions() as db:
                saved = await db.get(self.models.Interview, interview_id)
                self.assertEqual(saved.status, "completed")

        self.stop_voice.side_effect = stopped
        response = await self.client.post(
            f"/api/interview/{interview_id}/end", headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.stop_voice.assert_awaited_once()
        async with self.sessions.begin() as db:
            await self.completion.complete_from_callback(db, interview_id, transcript=deepcopy(TRANSCRIPT))
            job = await self.jobs.get_report_job(db, interview_id, user_id="owner")
            job.status = "ready"
            interview = await db.get(self.models.Interview, interview_id)
            interview.status = "evaluated"
        self.stop_voice.side_effect = None
        response = await self.client.post(
            f"/api/interview/{interview_id}/end", headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200)
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            self.assertEqual(interview.status, "evaluated")

    async def test_rejoining_active_interview_reuses_room_without_restarting_bot(self):
        room_url = "https://test.daily.co/room"
        interview_id = await self.make_interview(
            status="in_progress", transcript=None, daily_room_url=room_url,
            pipecat_session_id="existing-bot-session",
        )
        async with self.sessions() as db:
            owner = await db.get(self.models.User, "owner")
            credits_before = owner.credits_remaining
        with (
            patch.object(self.routes, "get_daily_token", new=AsyncMock(return_value="rejoin-token")) as token,
            patch.object(self.routes, "_start_voice_agent", new=AsyncMock()) as start,
            patch.object(self.routes, "create_daily_room", new=AsyncMock()) as create_room,
        ):
            response = await self.client.post(
                f"/api/interview/{interview_id}/start", headers=self.owner_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "room_url": room_url, "token": "rejoin-token", "status": "in_progress",
        })
        token.assert_awaited_once_with("room", is_owner=False)
        start.assert_not_awaited()
        create_room.assert_not_awaited()
        self.stop_voice.assert_not_awaited()
        async with self.sessions() as db:
            interview = await db.get(self.models.Interview, interview_id)
            owner = await db.get(self.models.User, "owner")
            self.assertEqual((interview.status, interview.daily_room_url), ("in_progress", room_url))
            self.assertEqual(interview.pipecat_session_id, "existing-bot-session")
            self.assertIsNone(interview.transcript)
            self.assertIsNone(await self.jobs.get_report_job(db, interview_id, user_id="owner"))
            self.assertEqual(owner.credits_remaining, credits_before)

    async def test_finished_interviews_cannot_restart_or_call_voice_services(self):
        with (
            patch.object(self.routes, "get_daily_token", new=AsyncMock()) as token,
            patch.object(self.routes, "_start_voice_agent", new=AsyncMock()) as start,
            patch.object(self.routes, "create_daily_room", new=AsyncMock()) as create_room,
        ):
            for status in ("completed", "evaluated", "incomplete", "failed"):
                with self.subTest(status=status):
                    interview_id = await self.make_interview(status=status)
                    response = await self.client.post(
                        f"/api/interview/{interview_id}/start", headers=self.owner_headers,
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    async with self.sessions() as db:
                        interview = await db.get(self.models.Interview, interview_id)
                        self.assertEqual(interview.status, status)
        token.assert_not_awaited()
        start.assert_not_awaited()
        create_room.assert_not_awaited()
        self.stop_voice.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
