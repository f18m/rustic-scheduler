"""Throwaway Docker containers for integration tests, on random local ports."""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

PUSHGATEWAY_IMAGE = "prom/pushgateway:v1.11.4"
PROMETHEUS_IMAGE = "prom/prometheus:v3.16.0"
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
S3_IMAGE = "versity/versitygw:v1.10.0"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


class Container:
    def __init__(
        self,
        image: str,
        port: int,
        *args: str,
        command: list[str] | None = None,
        fixed_port: bool = False,
    ) -> None:
        self.name = f"rs-test-{uuid.uuid4().hex[:10]}"
        self.internal_port = port
        # A fixed host port survives a container restart; a random one does not.
        host_port = str(free_port()) if fixed_port else ""
        docker(
            "run", "-d", "--name", self.name, "-p", f"127.0.0.1:{host_port}:{port}",
            "--add-host", "host.docker.internal:host-gateway", *args, image, *(command or []),
        )  # fmt: skip
        try:
            self.port = self._host_port()
        except Exception:
            self.remove()  # never leave a half-started container behind
            raise

    def _host_port(self) -> int:
        return int(
            docker("port", self.name, str(self.internal_port)).splitlines()[0].rsplit(":", 1)[1]
        )

    def stop(self) -> None:
        docker("stop", "-t", "2", self.name)

    def start(self) -> None:
        docker("start", self.name)
        self.port = self._host_port()

    def exec(self, *args: str) -> str:
        return docker("exec", self.name, *args)

    def logs(self) -> str:
        done = subprocess.run(
            ["docker", "logs", self.name], capture_output=True, text=True, check=False
        )
        return done.stdout + done.stderr

    def remove(self) -> None:
        docker("rm", "-f", "-v", self.name, check=False)

    def wait_http(self, path: str, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2):
                    return
            except urllib.error.HTTPError:
                return  # the server answers, which is all that is needed
            except OSError:
                time.sleep(0.3)
        raise RuntimeError(f"{self.name} did not come up:\n{self.logs()}")
