"""Standalone report worker: python -m app.worker [--once].

Uses the backend's database and AI configuration. Start after the backend has
created its tables. This command does not enqueue interviews or change routes.
"""

import argparse
import asyncio
import logging
import signal


logger = logging.getLogger(__name__)


async def run(*, once: bool, poll_interval: float) -> None:
    # Lazy imports keep --help free of configuration, secrets, and network I/O.
    from app.database import async_session, engine
    from app.services.report_worker import ReportWorker

    worker = ReportWorker(async_session)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
            installed_signals.append(sig)
        except NotImplementedError:
            pass
    try:
        while True:
            try:
                processed = await worker.run_once()
            except Exception as exc:
                logger.error("Unable to claim report work (%s)", type(exc).__name__)
                if once:
                    raise
                processed = False
            if once:
                return
            if not processed:
                await asyncio.sleep(poll_interval)
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process saved RoundReady report jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one eligible job, then exit.")
    parser.add_argument("--poll-interval", type=float, default=2, help="Seconds to wait when no job is eligible (default: 2).")
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(once=args.once, poll_interval=args.poll_interval))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Report worker stopped; unfinished claims will recover after their leases expire")


if __name__ == "__main__":
    main()
