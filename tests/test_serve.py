"""Process-supervisor checks with fake children; no servers are started."""

import asyncio
import signal
import unittest
from unittest.mock import AsyncMock, patch

from app import serve


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.exited = asyncio.Event()
        self.terminated = False
        self.killed = False
        if returncode is not None:
            self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.exited.set()


class BackendSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_child_exit_stops_other_child_and_returns_failure(self):
        for exited_index in (0, 1):
            with self.subTest(exited_index=exited_index):
                children = [FakeProcess(), FakeProcess()]
                children[exited_index] = FakeProcess(returncode=0)
                loop = asyncio.get_running_loop()
                with (
                    patch.object(serve.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=children)) as spawn,
                    patch.object(loop, "add_signal_handler"),
                    patch.object(loop, "remove_signal_handler") as remove,
                    patch.dict(serve.os.environ, {"PORT": "8765"}),
                ):
                    result = await asyncio.wait_for(serve.serve(), timeout=2)
                self.assertEqual(result, 1)
                self.assertTrue(children[1 - exited_index].terminated)
                self.assertFalse(children[exited_index].terminated)
                self.assertEqual(spawn.await_count, 2)
                self.assertIn("8765", spawn.await_args_list[0].args)
                self.assertIn("app.worker", spawn.await_args_list[1].args)
                self.assertEqual(remove.call_count, 2)

    async def test_sigterm_cancels_supervisor_and_stops_both_children(self):
        children = [FakeProcess(), FakeProcess()]
        handlers = {}
        both_started = asyncio.Event()
        spawn_count = 0

        async def spawn(*command):
            nonlocal spawn_count
            child = children[spawn_count]
            spawn_count += 1
            if spawn_count == 2:
                both_started.set()
            return child

        loop = asyncio.get_running_loop()
        with (
            patch.object(serve.asyncio, "create_subprocess_exec", side_effect=spawn),
            patch.object(loop, "add_signal_handler", side_effect=lambda sig, callback: handlers.update({sig: callback})),
            patch.object(loop, "remove_signal_handler") as remove,
        ):
            task = asyncio.create_task(serve.serve())
            try:
                await asyncio.wait_for(both_started.wait(), timeout=2)
                handlers[signal.SIGTERM]()
                self.assertEqual(await asyncio.wait_for(task, timeout=2), 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(all(child.terminated for child in children))
        self.assertFalse(any(child.killed for child in children))
        self.assertEqual(remove.call_count, 2)

    async def test_second_child_start_failure_stops_already_started_api(self):
        api = FakeProcess()
        loop = asyncio.get_running_loop()
        with (
            patch.object(serve.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=[api, OSError("Simulated spawn failure")])),
            patch.object(loop, "add_signal_handler"),
            patch.object(loop, "remove_signal_handler") as remove,
        ):
            with self.assertRaises(OSError):
                await asyncio.wait_for(serve.serve(), timeout=2)
        self.assertTrue(api.terminated)
        self.assertEqual(remove.call_count, 2)


if __name__ == "__main__":
    unittest.main()
