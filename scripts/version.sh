#!/usr/bin/env bash
# Prints the version of this checkout, derived from the Git tags (PEP 440):
# exactly the tag on a tagged commit, otherwise the next version with a .devN suffix.
# It is what the Python package reports and what the image build is given as VERSION.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --frozen --quiet hatchling version
