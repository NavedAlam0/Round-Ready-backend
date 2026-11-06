"""Committed PostgreSQL notifications using a disposable local database."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, text

from tests.postgres_support import PostgresTestCase


class ReportNotificationTests(PostgresTestCase):
    extra_modules = {"notifications": "app.services.report_notifications"}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.engine.begin() as connection:
            await self.notifications.install_report_notifications(connection)
        self.hub = self.notifications.ReportNotificationHub(
            self.engine, ping_seconds=0.1, ping_timeout_seconds=2,
            reconnect_base_seconds=0.01, reconnect_max_seconds=0.05,
        )
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.close()
        await super().asyncTearDown()

    async def queue_job(self, interview_id):
        async with self.sessions.begin() as db:
            job = await self.jobs.create_report_job(db, interview_id, user_id="owner")
            return job.id

    async def wait_for_event(self, event):
        await asyncio.wait_for(event.wait(), timeout=3)

    async def assert_quiet(self, event):
        # PostgreSQL delivers asynchronously after commit. This brief window
        # distinguishes absent notifications from merely delayed callbacks.
        await asyncio.sleep(0.08)
        self.assertFalse(event.is_set())

    async def test_commit_wakes_all_matching_watchers_only(self):
        interview_id = await self.make_interview()
        with self.hub.watch(interview_id) as first, self.hub.watch(interview_id) as second, self.hub.watch("other-interview") as other:
            await self.queue_job(interview_id)
            await self.wait_for_event(first)
            await self.wait_for_event(second)
            self.assertFalse(other.is_set())
        self.assertEqual(self.hub._watchers, {})
        self.assertEqual(self.hub._watcher_count, 0)

    async def test_changes_notify_only_after_commit_and_never_after_rollback(self):
        interview_id = await self.make_interview()
        with self.hub.watch(interview_id) as event:
            async with self.sessions() as db:
                await self.jobs.create_report_job(db, interview_id, user_id="owner")
                await self.assert_quiet(event)
                await db.rollback()
            await self.assert_quiet(event)
            async with self.sessions() as db:
                await self.jobs.create_report_job(db, interview_id, user_id="owner")
                await self.assert_quiet(event)
                await db.commit()
            await self.wait_for_event(event)

    async def test_job_progress_notifies_but_lease_heartbeats_do_not(self):
        interview_id = await self.make_interview()
        with self.hub.watch(interview_id) as event:
            job_id = await self.queue_job(interview_id)
            await self.wait_for_event(event)
            event.clear()
            async with self.sessions.begin() as db:
                job = await db.get(self.models.ReportJob, job_id)
                job.lease_token = "heartbeat-token"
                job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
                job.updated_at = datetime.now(timezone.utc)
            await self.assert_quiet(event)

            updates = (
                ("stage", "evaluating"),
                ("status", "retrying"),
                ("last_error_code", "provider_unavailable"),
                ("last_error_message", "Temporary interruption"),
                ("evaluation_result", {"saved": True}),
            )
            for name, value in updates:
                with self.subTest(field=name):
                    event.clear()
                    async with self.sessions.begin() as db:
                        job = await db.get(self.models.ReportJob, job_id)
                        setattr(job, name, value)
                    await self.wait_for_event(event)

    async def test_interview_inputs_and_deletion_notify(self):
        interview_id = await self.make_interview()
        updates = (
            ("status", "evaluated"),
            ("transcript", [{"role": "user", "text": "Updated final transcript."}]),
            ("code_finalized_at", datetime.now(timezone.utc)),
        )
        with self.hub.watch(interview_id) as event:
            for name, value in updates:
                with self.subTest(field=name):
                    event.clear()
                    async with self.sessions.begin() as db:
                        interview = await db.get(self.models.Interview, interview_id)
                        setattr(interview, name, value)
                    await self.wait_for_event(event)
            event.clear()
            async with self.sessions.begin() as db:
                await db.execute(delete(self.models.Interview).where(self.models.Interview.id == interview_id))
            await self.wait_for_event(event)

    async def test_job_deletion_notifies(self):
        interview_id = await self.make_interview()
        with self.hub.watch(interview_id) as event:
            job_id = await self.queue_job(interview_id)
            await self.wait_for_event(event)
            event.clear()
            async with self.sessions.begin() as db:
                await db.execute(delete(self.models.ReportJob).where(self.models.ReportJob.id == job_id))
            await self.wait_for_event(event)

    async def test_installation_is_safe_when_api_processes_start_together(self):
        async def install_again():
            async with self.engine.begin() as connection:
                await self.notifications.install_report_notifications(connection)

        await asyncio.wait_for(asyncio.gather(install_again(), install_again()), timeout=5)
        interview_id = await self.make_interview()
        with self.hub.watch(interview_id) as event:
            await self.queue_job(interview_id)
            await self.wait_for_event(event)

    async def test_lost_connection_wakes_watchers_and_reconnects(self):
        interview_id = await self.make_interview()
        original_pid = self.hub._driver_connection.get_server_pid()
        with self.hub.watch(interview_id) as event:
            async with self.engine.begin() as connection:
                await connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": original_pid})
            await self.wait_for_event(event)

            async def wait_for_reconnect():
                while True:
                    driver = self.hub._driver_connection
                    if self.hub.connected and driver is not None and driver.get_server_pid() != original_pid:
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_reconnect(), timeout=5)
            event.clear()
            await self.queue_job(interview_id)
            await self.wait_for_event(event)

    async def test_close_releases_connection_wakes_watchers_and_rejects_new_ones(self):
        with self.hub.watch("interview") as event:
            await self.hub.close()
            self.assertTrue(event.is_set())
            self.assertFalse(self.hub.connected)
            self.assertIsNone(self.hub._task)
            self.assertEqual(self.engine.pool.checkedout(), 0)
            self.assertEqual(self.hub._watcher_count, 0)
        with self.assertRaises(RuntimeError):
            with self.hub.watch("interview"):
                pass

    async def test_watcher_limit_counts_subscribers_and_unsubscribes_on_error(self):
        hub = self.notifications.ReportNotificationHub(self.engine, max_watchers=2)
        try:
            with ExitStack() as watchers:
                watchers.enter_context(hub.watch("same-interview"))
                watchers.enter_context(hub.watch("same-interview"))
                with self.assertRaises(self.notifications.ReportWatchLimitError):
                    with hub.watch("different-interview"):
                        pass
            self.assertEqual(hub._watchers, {})
            with self.assertRaises(ValueError):
                with hub.watch("interview"):
                    raise ValueError("Simulated disconnected stream")
            self.assertEqual(hub._watcher_count, 0)
        finally:
            await hub.close()
