"""Optional browser interface, on its own port.

  GET  /                                    the page (plus /app.js, /app.css)
  GET  /api/v1/overview                     everything the page shows, as JSON
  POST /api/v1/jobs/{job}/{operation}/run   start an operation

The metrics port stays read-only; this listener is the one that can start
work, so it is separate, off by default and limited to ``web.operations``.
A request is handled exactly like ``rustic-scheduler trigger``: forget and
prune must also be enabled on the job.

Protection (see docs/web.md):
* HTTP Basic authentication when ``web.password_file`` is set;
* POST needs a custom header and a same-origin ``Origin``, so that another web
  site open in the same browser cannot start a run;
* a strict Content-Security-Policy, and a page that never builds HTML from data.

History is the engine's in-memory list: nothing is stored for this interface.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import shlex
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import WebConfig
from .engine import Engine
from .model import Operation, Trigger
from .redact import REDACTOR

log = logging.getLogger(__name__)

#: Header the page sends with every POST. A cross-site form cannot set it, and
#: a cross-site script would need a CORS preflight that is never granted.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "rustic-scheduler"
_ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
}
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_DESTRUCTIVE = {Operation.FORGET, Operation.PRUNE}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def manual_instructions(engine: Engine, job: str) -> dict[str, Any]:
    """What is needed to run Rustic by hand on a job's repository. No secret values."""
    runtime = engine.runtimes[job]
    scope = ["--filter-label", runtime.label]
    if runtime.job.host:
        scope += ["--filter-host", runtime.job.host]
    # Only what the configuration sets; the inherited environment is not shown.
    env = {**engine.config.rustic.env, **runtime.job.env}
    return {
        "label": runtime.label,
        "host": runtime.job.host,
        "scope": shlex.join(scope),
        "profiles": shlex.join(runtime.base_args),
        "helper": f"rustic-scheduler rustic {shlex.quote(job)} --",
        "env": [REDACTOR(f"{name}={shlex.quote(value)}") for name, value in sorted(env.items())],
    }


def overview(engine: Engine, config: WebConfig, rustic_version: str) -> dict[str, Any]:
    status = engine.status()
    allowed = set(config.operations)
    jobs = []
    for name, job in status["jobs"].items():
        enabled = engine.enabled_operations(name)
        rows = []
        for operation in Operation:
            row = job["operations"].get(str(operation))
            if row is None and operation not in enabled:
                continue
            row = row or {
                "state": engine.is_active(name, operation) or "idle",
                "started_at": None,
                "trigger": None,
                "next_run": None,
                "schedule": None,
                "progress": None,
                "last_result": None,
                "last_skip": None,
            }
            rows.append(
                row
                | {
                    "operation": str(operation),
                    "can_run": operation in enabled and operation.value in allowed,
                    "destructive": operation in _DESTRUCTIVE,
                }
            )
        jobs.append(
            {
                "name": name,
                "label": job["label"],
                "host": job["host"],
                "repository_busy": job["repository_busy"],
                "operations": rows,
                "manual": manual_instructions(engine, name),
            }
        )
    return {
        "version": __version__,
        "rustic_version": rustic_version,
        "now": engine.clock().isoformat(),
        "ready": status["ready"],
        "shutting_down": status["shutting_down"],
        "started_at": status["started_at"],
        "history_size": config.history_size,
        "jobs": jobs,
        "history": [result.to_dict() for result in reversed(engine.history)],
    }


def _password(config: WebConfig) -> str | None:
    """The expected password, read on every request so a rotated secret is picked up."""
    if config.password_file is None:
        return None
    password = config.password_file.read_text(encoding="utf-8").strip()
    REDACTOR.register([password])
    return password


def _authorised(request: web.Request, username: str, password: str) -> bool:
    scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(given, f"{username}:{password}".encode())


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # not a browser, or a browser that omits it for same-origin requests
    return urlsplit(origin).netloc.lower() == request.headers.get("Host", "").lower()


def create_app(engine: Engine, config: WebConfig, rustic_version: str) -> web.Application:
    metrics = engine.metrics
    for result in ("accepted", "rejected"):
        metrics.web_commands.labels(result)
    files = resources.files("rustic_scheduler") / "webui"
    assets = {
        path: (files.joinpath(name).read_bytes(), content_type)
        for path, (name, content_type) in _ASSETS.items()
    }

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        response: web.StreamResponse
        try:
            password = _password(config)
        except OSError as exc:
            # Never fall back to "no password" when the secret cannot be read.
            log.error("web interface: cannot read web.password_file: %s", exc.strerror or exc)
            response = web.json_response(
                {"message": "the password file cannot be read; see the scheduler log"}, status=503
            )
        else:
            if password is not None and not _authorised(request, config.username, password):
                response = web.Response(
                    status=401,
                    text="authentication required\n",
                    headers={"WWW-Authenticate": 'Basic realm="rustic-scheduler", charset="UTF-8"'},
                )
            else:
                try:
                    response = await handler(request)
                except web.HTTPException as exc:
                    exc.headers.update(_HEADERS)
                    raise
        response.headers.update(_HEADERS)
        return response

    async def asset(request: web.Request) -> web.Response:
        body, content_type = assets[request.path]
        return web.Response(body=body, content_type=content_type, charset="utf-8")

    async def get_overview(_: web.Request) -> web.Response:
        return web.json_response(overview(engine, config, rustic_version))

    def refuse(status: int, message: str) -> web.Response:
        metrics.web_commands.labels("rejected").inc()
        if status != 409:  # a 409 is logged by engine.reject, with the job
            log.warning("web interface: request refused: %s", message)
        return web.json_response({"accepted": False, "message": message}, status=status)

    async def run(request: web.Request) -> web.Response:
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE or not _same_origin(request):
            return refuse(403, "this request did not come from the scheduler's own page")
        job = request.match_info["job"]
        try:
            operation = Operation(request.match_info["operation"])
        except ValueError:
            return refuse(404, "unknown operation")
        if job not in engine.runtimes:
            return refuse(404, "unknown job")
        state = engine.is_active(job, operation)
        if operation.value not in config.operations:
            refusal = f"{operation} is not listed in web.operations"
        elif state is not None:
            # Not recorded as a skipped run: a second click is not a missed backup.
            refusal = f"{operation} for job {job} is already {state}"
        else:
            accepted, message = engine.trigger(job, operation, Trigger.WEB)
            if accepted:
                metrics.web_commands.labels("accepted").inc()
                log.info("job %s: %s requested from the web interface", job, operation)
                return web.json_response({"accepted": True, "message": message}, status=202)
            refusal = message
        engine.reject(job, operation, Trigger.WEB, refusal)
        return refuse(409, refusal)

    app = web.Application(middlewares=[guard])
    for path in _ASSETS:
        app.router.add_get(path, asset)
    app.router.add_get("/api/v1/overview", get_overview)
    app.router.add_post("/api/v1/jobs/{job}/{operation}/run", run)
    return app


class WebServer:
    def __init__(self, engine: Engine, config: WebConfig, rustic_version: str) -> None:
        self.engine = engine
        self.config = config
        self.rustic_version = rustic_version
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        config = self.config
        runner = web.AppRunner(
            create_app(self.engine, config, self.rustic_version), access_log=None
        )
        await runner.setup()
        site = web.TCPSite(runner, config.host, config.port)
        try:
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        log.info(
            "web interface on http://%s:%s/ (may start: %s; authentication: %s)",
            config.host,
            config.port,
            ", ".join(config.operations) or "nothing",
            "password" if config.password_file else "none",
        )
        if config.password_file is None:
            log.warning(
                "web interface: no web.password_file, so anyone who can reach port %s can "
                "start the operations above",
                config.port,
            )
        elif not os.access(config.password_file, os.R_OK):
            log.error("web interface: web.password_file is not readable; every request will fail")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
