"""Start-up: the first log line and the start-up notification, run through serve()."""

import asyncio
import json
import logging
import os
import signal
import socket

from aiohttp import web

from rustic_scheduler import SUPPORTED_RUSTIC_VERSION, __version__
from rustic_scheduler.app import serve


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def wait_until(condition, seconds: float = 20) -> None:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)


async def test_first_log_line_names_both_versions_and_startup_is_announced(
    sandbox, short_tmp, capsys
):
    received: list[dict] = []

    async def hook(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/hook", hook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    conf = short_tmp / "apprise.conf"
    conf.write_text(f"json://127.0.0.1:{runner.addresses[0][1]}/hook\n")
    # No events selected: the start-up notification must not depend on them.
    sandbox.config(
        http={"host": "127.0.0.1", "port": free_port()},
        notifications={"enabled": True, "config_files": [str(conf)], "retries": 0, "events": []},
    )
    path = short_tmp / "config.json"
    path.write_text(json.dumps(sandbox.last_raw))

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    task = asyncio.create_task(serve(path))
    try:
        await wait_until(lambda: bool(received) or task.done())
        if not task.done():
            os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 30) == 0
    finally:
        root.handlers[:], root.level = saved_handlers, saved_level
        await runner.cleanup()

    first_line = capsys.readouterr().err.splitlines()[0]
    assert first_line.endswith(
        f"INFO    rustic_scheduler.app: Starting rustic-scheduler {__version__}"
        f" with Rustic {SUPPORTED_RUSTIC_VERSION}"
    )
    [notice] = received
    assert notice["title"] == f"rustic-scheduler {__version__} started"
    assert notice["message"] == (
        f"Version: {__version__}\nRustic: {SUPPORTED_RUSTIC_VERSION}\nJobs: 1"
    )
    assert notice["type"] == "info"
