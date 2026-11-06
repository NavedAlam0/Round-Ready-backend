"""Run the API and durable worker together in the deployed backend container.

If either process exits unexpectedly, stop the other and exit nonzero so the
container supervisor can restart both. Local development may run them in two
separate terminals instead. No application credentials are loaded here.
"""

import asyncio
import logging
import os
import signal
import sys


logger = logging.getLogger(__name__)


async def _stop(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def serve() -> int:
    port = int(os.environ.get("PORT", "8000"))
    if not 0 < port < 65536:
        raise ValueError("PORT must be between 1 and 65535")
    children = []
    waiters = []
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    signals = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, current.cancel)
            signals.append(sig)
        except NotImplementedError:
            pass
    try:
        for command in (
            (sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)),
            (sys.executable, "-m", "app.worker"),
        ):
            children.append(await asyncio.create_subprocess_exec(*command))
        waiters = [asyncio.create_task(child.wait()) for child in children]
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        logger.error("Backend child process exited unexpectedly (%s)", next(iter(done)).result())
        return 1
    except asyncio.CancelledError:
        return 0
    finally:
        await asyncio.gather(*(_stop(child) for child in children))
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        for sig in signals:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(serve()))
