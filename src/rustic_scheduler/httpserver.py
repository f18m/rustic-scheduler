"""HTTP endpoints.

TCP (read-only, meant for Prometheus and probes):
  GET /metrics         scheduler metrics
  GET /healthz         liveness: the scheduling loop is iterating
  GET /readyz          readiness: the scheduler can launch scheduled work
  GET /api/v1/status   detailed JSON status, including diagnostics

Unix socket in the state directory (mode 0600, same user only):
  the read-only routes above, plus
  POST /api/v1/jobs/{job}/{operation}/run   manual invocation

Keeping the only mutating route off the network means that reaching the
metrics port never allows anyone to start, or interfere with, a backup.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST

from .engine import Engine
from .model import Operation

log = logging.getLogger(__name__)


def _routes(engine: Engine, *, control: bool) -> web.Application:
    app = web.Application()

    async def metrics(_: web.Request) -> web.Response:
        engine.metrics.ready.set(1 if engine.ready else 0)
        return web.Response(
            body=engine.metrics.render(), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    async def healthz(_: web.Request) -> web.Response:
        # Deliberately derived from the loop heartbeat, not from "the HTTP
        # server answers": a stuck scheduler must turn unhealthy.
        alive = engine.ready or engine.stop_event.is_set()
        return web.json_response(
            {"status": "ok" if alive else "scheduler loop is not running"},
            status=200 if alive else 503,
        )

    async def readyz(_: web.Request) -> web.Response:
        ready = engine.ready
        body = {
            "ready": ready,
            "shutting_down": engine.stop_event.is_set(),
            "heartbeat": engine.heartbeat.isoformat() if engine.heartbeat else None,
            "jobs": len(engine.runtimes),
        }
        return web.json_response(body, status=200 if ready else 503)

    async def status(_: web.Request) -> web.Response:
        return web.json_response(engine.status())

    async def run(request: web.Request) -> web.Response:
        job = request.match_info["job"]
        try:
            operation = Operation(request.match_info["operation"])
        except ValueError:
            return web.json_response(
                {"accepted": False, "message": "unknown operation"}, status=404
            )
        accepted, message = engine.trigger(job, operation)
        return web.json_response(
            {"accepted": accepted, "message": message}, status=202 if accepted else 409
        )

    app.router.add_get("/metrics", metrics)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/api/v1/status", status)
    if control:
        app.router.add_post("/api/v1/jobs/{job}/{operation}/run", run)
    return app


class HttpServer:
    def __init__(self, engine: Engine, host: str, port: int, socket_path: Path | None) -> None:
        self.engine = engine
        self.host = host
        self.port = port
        self.socket_path = socket_path
        self._runners: list[web.AppRunner] = []

    async def start(self) -> None:
        public = web.AppRunner(_routes(self.engine, control=False), access_log=None)
        await public.setup()
        await web.TCPSite(public, self.host, self.port).start()
        self._runners.append(public)
        log.info("serving metrics on http://%s:%s/metrics", self.host, self.port)
        if self.socket_path is None:
            log.warning("no writable state directory: manual triggers are unavailable")
            return
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        control = web.AppRunner(_routes(self.engine, control=True), access_log=None)
        await control.setup()
        previous = os.umask(0o177)
        try:
            await web.UnixSite(control, str(self.socket_path)).start()
        except OSError as exc:
            # For example a state directory path too long for a Unix socket.
            log.warning(
                "cannot open the control socket %s (%s): manual triggers are unavailable",
                self.socket_path,
                exc,
            )
            await control.cleanup()
            self.socket_path = None
            return
        finally:
            os.umask(previous)
        self._runners.append(control)

    async def stop(self) -> None:
        for runner in self._runners:
            await runner.cleanup()
        if self.socket_path is not None:
            with contextlib.suppress(OSError):
                self.socket_path.unlink()
