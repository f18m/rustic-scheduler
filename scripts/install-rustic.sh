#!/bin/sh
# Download the pinned Rustic release for this machine and verify its SHA-256.
# Usage: scripts/install-rustic.sh [DEST_DIR]   (default: .tools)
set -eu

VERSION="0.12.0"
DEST="${1:-.tools}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)               TARGET="x86_64-unknown-linux-musl";  SHA256="c7dc0772bb6e81f9acdcd7c2642c076c99455b20e3a57b2dfc1b7caee0035184" ;;
  Linux-aarch64|Linux-arm64)  TARGET="aarch64-unknown-linux-musl"; SHA256="192d0701216edecba4d523b5ce0e1183b3904140791789ee4974bf70b41b483b" ;;
  Darwin-arm64)               TARGET="aarch64-apple-darwin";       SHA256="a8f7ddf306bdad9dca7670fba9a009509300190f84fe7fb8efde5997ccd37348" ;;
  Darwin-x86_64)              TARGET="x86_64-apple-darwin";        SHA256="39e71b0c745a416de6258018e5a74cf5ee80749a6e14a55b686dd671d9ec529a" ;;
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
