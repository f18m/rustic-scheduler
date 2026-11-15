#!/bin/sh
# Download the pinned Rustic release for this machine and verify its SHA-256.
# Usage: scripts/install-rustic.sh [DEST_DIR]   (default: .tools)
set -eu

VERSION="0.11.4"
DEST="${1:-.tools}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)               TARGET="x86_64-unknown-linux-musl";  SHA256="628b26785f3787fa1d52e61b3289493f483230a1ed6e86a84ae9c76d7a41a81e" ;;
  Linux-aarch64|Linux-arm64)  TARGET="aarch64-unknown-linux-musl"; SHA256="35ccd40ff565b9760245dd391f9fef04c46778ce5e073d833288abe2e5eabd20" ;;
  Darwin-arm64)               TARGET="aarch64-apple-darwin";       SHA256="6df166f68876f1bd1e0965547542374877f04ab498cbb5e19b1b155844bce307" ;;
  Darwin-x86_64)              TARGET="x86_64-apple-darwin";        SHA256="ff0014a53f10965bc7852945ad4ac52532ad8af0efe5e4f4957dd330889946de" ;;
  *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

ARCHIVE="rustic-v${VERSION}-${TARGET}.tar.gz"
URL="https://github.com/rustic-rs/rustic/releases/download/v${VERSION}/${ARCHIVE}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "downloading ${URL}"
curl --fail --silent --show-error --location --retry 3 -o "${WORK}/${ARCHIVE}" "$URL"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "${WORK}/${ARCHIVE}" | cut -d' ' -f1)"
else
  ACTUAL="$(shasum -a 256 "${WORK}/${ARCHIVE}" | cut -d' ' -f1)"
fi
if [ "$ACTUAL" != "$SHA256" ]; then
  echo "checksum mismatch for ${ARCHIVE}: expected ${SHA256}, got ${ACTUAL}" >&2
  exit 1
fi

mkdir -p "$DEST"
tar -xzf "${WORK}/${ARCHIVE}" -C "$WORK" rustic
install -m 0755 "${WORK}/rustic" "${DEST}/rustic"
echo "installed $("${DEST}/rustic" --version) to ${DEST}/rustic"
