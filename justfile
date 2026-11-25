# Developer tasks. Run `just` to list them.

default:
    @just --list

# Install Python dependencies and the pinned Rustic binary into .tools/
setup:
    uv sync
    ./scripts/install-rustic.sh .tools

# Formatting, lint and type checks
lint:
    uv run ruff format --check src tests
    uv run ruff check src tests
    uv run mypy

# Apply formatting and safe lint fixes
fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Unit tests only (no Rustic binary, no Docker)
test-unit:
    uv run pytest tests/unit

# Unit and integration tests; integration tests need .tools/rustic and, for some, Docker
test:
    uv run pytest

# Like `test`, but missing Rustic or Docker is an error instead of a skip
test-strict:
    REQUIRE_INTEGRATION=1 uv run pytest

# Build the image and test it with the Pushgateway sidecar and Prometheus
e2e:
    ./tests/e2e/run.sh

# Stop and remove the e2e containers and volumes, including those left by `tests/e2e/run.sh --keep`
e2e-down:
    docker compose -f tests/e2e/compose.e2e.yaml down -v --remove-orphans

# Build the container image for the local architecture
build tag="rustic-scheduler:dev":
    docker build --build-arg VERSION="$(./scripts/version.sh)" -f docker/Dockerfile -t {{tag}} .

# Build for both supported architectures (needs a buildx builder with emulation)
build-multiarch tag="rustic-scheduler:dev":
    docker buildx build --build-arg VERSION="$(./scripts/version.sh)" --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t {{tag}} .

# Print the version of this checkout, derived from the Git tags
version:
    @./scripts/version.sh

# Validate a configuration file with the local Rustic binary
validate config="examples/config.yaml":
    uv run rustic-scheduler -c {{config}} validate

# Run the scheduler with its web interface on a throwaway repository (needs `just setup`)
manual-test:
    #!/usr/bin/env bash
    set -euo pipefail
    dir=/tmp/rustic-scheduler-manual
    mkdir -p "$dir/src" "$dir/repo"
    [ -f "$dir/password" ] || echo "manual-test-password" > "$dir/password"
    [ -f "$dir/src/file.txt" ] || echo hello > "$dir/src/file.txt"
    [ -f "$dir/repo/config" ] || .tools/rustic -P tests/manual/profiles/repo.toml init
    uv run rustic-scheduler -c tests/manual/config.yaml run

# Everything CI runs
ci: lint test-strict e2e
