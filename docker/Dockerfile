# syntax=docker/dockerfile:1
#
# rustic-scheduler image. Every external input is pinned:
#   - base image by tag and multi-arch digest
#   - Rustic by version and per-architecture SHA-256
#   - uv by version and digest, Python packages by uv.lock (hashes)
# Supported platforms: linux/amd64, linux/arm64.

ARG PYTHON_IMAGE=python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
# The package version, normally `scripts/version.sh` (the Git tag). The build has no
# Git history to derive it from.
ARG VERSION=0.0.0.dev0
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85

FROM ${UV_IMAGE} AS uv

# ---- Rustic: download the static musl build and verify its checksum ----------
FROM ${PYTHON_IMAGE} AS rustic
ARG RUSTIC_VERSION=0.11.4
ARG RUSTIC_SHA256_AMD64=628b26785f3787fa1d52e61b3289493f483230a1ed6e86a84ae9c76d7a41a81e
ARG RUSTIC_SHA256_ARM64=35ccd40ff565b9760245dd391f9fef04c46778ce5e073d833288abe2e5eabd20
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) target="x86_64-unknown-linux-musl";  sha="${RUSTIC_SHA256_AMD64}" ;; \
      arm64) target="aarch64-unknown-linux-musl"; sha="${RUSTIC_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="rustic-v${RUSTIC_VERSION}-${target}.tar.gz"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "https://github.com/rustic-rs/rustic/releases/download/v${RUSTIC_VERSION}/${archive}" "/tmp/${archive}"; \
    echo "${sha}  /tmp/${archive}" | sha256sum -c -; \
    mkdir /out; tar -xzf "/tmp/${archive}" -C /out rustic; chmod 0755 /out/rustic; \
    /out/rustic --version

# ---- Python dependencies, exactly as locked ---------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
# Set only now, after the dependencies are installed: it applies to this project alone.
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
RUN uv sync --frozen --no-dev --no-editable

# ---- Runtime ---------------------------------------------------------------
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="rustic-scheduler" \
      org.opencontainers.image.description="Schedules Rustic backups, with Prometheus, MQTT and Apprise reporting" \
      org.opencontainers.image.licenses="MIT"

COPY --from=rustic /out/rustic /usr/local/bin/rustic

# A second copy of Rustic carries the CAP_DAC_READ_SEARCH file capability, so an
# unprivileged user can read every file of a read-only source mount. It can only
# be executed when the container is granted that capability (cap_add); the
# scheduler falls back to the plain binary otherwise. See docs/security.md.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates libcap2-bin; \
    install -D -m 0755 /usr/local/bin/rustic /usr/local/libexec/rustic-scheduler/rustic; \
    setcap cap_dac_read_search=+ep /usr/local/libexec/rustic-scheduler/rustic; \
    apt-get purge -y libcap2-bin; apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 1000 rustic; \
    useradd --uid 1000 --gid 1000 --home-dir /home/rustic --create-home --shell /usr/sbin/nologin rustic; \
    install -d -o 1000 -g 1000 -m 0700 /var/lib/rustic-scheduler /var/cache/rustic; \
    install -d -m 0755 /etc/rustic-scheduler/profiles

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUSTIC_SCHEDULER_CONFIG=/etc/rustic-scheduler/config.yaml \
    XDG_CACHE_HOME=/var/cache

USER 1000:1000
WORKDIR /home/rustic
VOLUME ["/var/lib/rustic-scheduler", "/var/cache/rustic"]
EXPOSE 9300 9301

# Ready means "the scheduling loop is alive", not merely "the port answers".
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["rustic-scheduler", "healthcheck"]

ENTRYPOINT ["rustic-scheduler"]
CMD ["run"]
