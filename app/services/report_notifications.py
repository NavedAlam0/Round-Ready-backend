"""Wake report subscribers from committed PostgreSQL changes.

The database remains the source of report state. Notifications carry only an
interview ID and can be coalesced or missed during a disconnect, so subscribers
read current state on subscription and whenever their event is set. Ownership
checks belong to the HTTP route, not this transport.
"""

import asyncio
from contextlib import contextmanager
import logging

from sqlalchemy import text


logger = logging.getLogger(__name__)
CHANNEL = "report_updates"


async def install_report_notifications(connection) -> None:
    """Install transactional notification triggers inside the caller's transaction.

    Run after schema creation/migrations. Concurrent API processes serialize
    installation through a transaction-scoped advisory lock.
    """
    await connection.execute(text("SELECT pg_advisory_xact_lock(1381126228, 1)"))
    await connection.execute(text("""
        CREATE OR REPLACE FUNCTION notify_report_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target_id text;
        BEGIN
            IF TG_TABLE_NAME = 'report_jobs' THEN
                IF TG_OP = 'DELETE' THEN
                    target_id := OLD.interview_id;
                ELSE
                    target_id := NEW.interview_id;
                END IF;
            ELSE
                IF TG_OP = 'DELETE' THEN
                    target_id := OLD.id;
                ELSE
                    target_id := NEW.id;
                END IF;
            END IF;
            PERFORM pg_notify('report_updates', target_id);
            RETURN NULL;
        END;
        $$
    """))
    await connection.execute(text("DROP TRIGGER IF EXISTS report_jobs_notify ON report_jobs"))
    await connection.execute(text("""
        CREATE TRIGGER report_jobs_notify
        AFTER INSERT OR DELETE OR UPDATE OF
            status, stage, last_error_code, last_error_message, evaluation_result
        ON report_jobs FOR EACH ROW EXECUTE FUNCTION notify_report_update()
    """))
    await connection.execute(text("DROP TRIGGER IF EXISTS interviews_report_notify ON interviews"))
    await connection.execute(text("""
        CREATE TRIGGER interviews_report_notify
        AFTER DELETE OR UPDATE OF status, transcript, code_finalized_at
        ON interviews FOR EACH ROW EXECUTE FUNCTION notify_report_update()
    """))


class ReportWatchLimitError(RuntimeError):
    """This API process already has its maximum number of report subscribers."""


class ReportNotificationHub:
    """One LISTEN connection per API process, shared by all report streams."""

    def __init__(
        self,
        engine,
        *,
        ping_seconds: float = 30,
        ping_timeout_seconds: float = 10,
        reconnect_base_seconds: float = 0.5,
        reconnect_max_seconds: float = 10,
        start_timeout_seconds: float = 10,
        max_watchers: int = 1000,
    ):
        if min(ping_seconds, ping_timeout_seconds, reconnect_base_seconds, start_timeout_seconds) <= 0:
            raise ValueError("Notification timeouts and intervals must be positive")
        if reconnect_max_seconds < reconnect_base_seconds or max_watchers < 1:
            raise ValueError("Invalid notification reconnect limit or subscriber limit")
        self.engine = engine
        self.ping_seconds = ping_seconds
        self.ping_timeout_seconds = ping_timeout_seconds
        self.reconnect_base_seconds = reconnect_base_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.start_timeout_seconds = start_timeout_seconds
        self.max_watchers = max_watchers
        self._watchers: dict[str, set[asyncio.Event]] = {}
        self._watcher_count = 0
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._driver_connection = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._ready.is_set() and not self._closed

    async def start(self) -> None:
        """Start once and wait until LISTEN is active before serving streams."""
        if self._closed:
            raise RuntimeError("Report notification hub is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="report-notifications")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.start_timeout_seconds)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Unsubscribe and release the dedicated connection and all watchers."""
        self._closed = True
        self._wake_all()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._ready.clear()
        self._watchers.clear()
        self._watcher_count = 0

    @contextmanager
    def watch(self, interview_id: str):
        """Register before reading state; clear the event before each state read."""
        if self._closed:
            raise RuntimeError("Report notification hub is closed")
        if self._watcher_count >= self.max_watchers:
            raise ReportWatchLimitError("Too many active report streams")
        event = asyncio.Event()
        self._watchers.setdefault(interview_id, set()).add(event)
        self._watcher_count += 1
        try:
            yield event
        finally:
            watchers = self._watchers.get(interview_id)
            if watchers is not None and event in watchers:
                watchers.remove(event)
                self._watcher_count -= 1
                if not watchers:
                    del self._watchers[interview_id]

    def _on_notification(self, connection, process_id, channel, payload) -> None:
        for event in self._watchers.get(payload, ()):
            event.set()

    def _wake_all(self) -> None:
        for watchers in self._watchers.values():
            for event in watchers:
                event.set()

    async def _listen(self) -> None:
        delay = self.reconnect_base_seconds
        while True:
            try:
                # SQLAlchemy owns checkout/return; asyncpg owns this connection's
                # LISTEN and health checks. No long-running transaction is held.
                async with self.engine.connect() as connection:
                    raw = await connection.get_raw_connection()
                    driver = raw.driver_connection
                    terminated = asyncio.Event()

                    def on_termination(driver_connection):
                        terminated.set()

                    driver.add_termination_listener(on_termination)
                    try:
                        await driver.add_listener(CHANNEL, self._on_notification)
                        self._driver_connection = driver
                        self._ready.set()
                        self._wake_all()
                        delay = self.reconnect_base_seconds
                        while True:
                            try:
                                await asyncio.wait_for(terminated.wait(), timeout=self.ping_seconds)
                            except TimeoutError:
                                await asyncio.wait_for(
                                    driver.fetchval("SELECT 1"), timeout=self.ping_timeout_seconds,
                                )
                            else:
                                raise ConnectionError("Report notification connection closed")
                    finally:
                        self._driver_connection = None
                        self._ready.clear()
                        self._wake_all()
                        driver.remove_termination_listener(on_termination)
                        if driver.is_closed():
                            await connection.invalidate()
                        else:
                            try:
                                await asyncio.wait_for(
                                    driver.remove_listener(CHANNEL, self._on_notification),
                                    timeout=self.ping_timeout_seconds,
                                )
                            except Exception:
                                # Never return a connection with a lingering
                                # callback into the pool after cleanup failed.
                                await connection.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ready.clear()
                self._wake_all()
                logger.warning("Report notifications disconnected (%s); reconnecting", type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_seconds)
