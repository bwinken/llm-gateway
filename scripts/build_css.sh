#!/usr/bin/env bash
# Build app/static/css/tailwind.css, the stylesheet of every web page, from
# the class names used in app/templates/ (settings: frontend/tailwind.config.js).
#
#   bash scripts/build_css.sh           rebuild it (run after changing a template)
#   bash scripts/build_css.sh --check   only say whether it is up to date (CI runs this)
#
# The first run downloads Tailwind's standalone program (one file, no Node
# needed) from GitHub into .cache/ and checks its SHA-256; later runs reuse
# it. Beginner's walk-through: docs/frontend-build.md.
set -euo pipefail

TAILWIND_VERSION="3.4.17"
# From https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/sha256sums.txt
sha256_for() {
    case "$1" in
        linux-x64)         echo "7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4" ;;
        linux-arm64)       echo "69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b" ;;
        macos-x64)         echo "6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3" ;;
        macos-arm64)       echo "a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3" ;;
        windows-x64.exe)   echo "67f1c5e3f5a03406a7bf5badf5ada09b79f3ae78ec43450c15f7e983068da346" ;;
        windows-arm64.exe) echo "76f516476784c00f1562160b5758e3d8f0e6c48957efb26b5b50fbdfd76aa382" ;;
        *) return 1 ;;
    esac
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/frontend/tailwind.config.js"
INPUT="$ROOT/frontend/tailwind.input.css"
OUTPUT="$ROOT/app/static/css/tailwind.css"

check=0
case "${1:-}" in
    "") ;;
    --check) check=1 ;;
    *) echo "Usage: bash scripts/build_css.sh [--check]" >&2; exit 2 ;;
esac

# Which build of the program this machine needs.
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    MINGW*|MSYS*|CYGWIN*) os=windows ;;
    *) echo "Unsupported system: $os" >&2; exit 1 ;;
esac
case "$arch" in
    x86_64|amd64) arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "Unsupported processor: $arch" >&2; exit 1 ;;
esac
platform="$os-$arch"
[ "$os" = windows ] && platform="$platform.exe"
want="$(sha256_for "$platform")" || { echo "No Tailwind build for $platform" >&2; exit 1; }

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

BIN="$ROOT/.cache/tailwindcss-v$TAILWIND_VERSION-$platform"
if [ ! -x "$BIN" ] || [ "$(sha256_of "$BIN")" != "$want" ]; then
    mkdir -p "$ROOT/.cache"
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/v$TAILWIND_VERSION/tailwindcss-$platform"
    echo "Downloading Tailwind $TAILWIND_VERSION ($platform) from GitHub..."
    curl -fsSL "$url" -o "$BIN.part"
    got="$(sha256_of "$BIN.part")"
    if [ "$got" != "$want" ]; then
        rm -f "$BIN.part"
        echo "Checksum mismatch: expected $want, got $got. Not using the download." >&2
        exit 1
    fi
    chmod +x "$BIN.part"
    mv "$BIN.part" "$BIN"
fi

build() {
    # --minify: one small file. The output depends only on the templates, the
    # config and the version, so every machine builds the same bytes.
    "$BIN" --config "$CONFIG" --input "$INPUT" --output "$1" --minify 2>"$ROOT/.cache/tailwind.log" || {
        cat "$ROOT/.cache/tailwind.log" >&2
        rm -f "$1"
        exit 1
    }
}

if [ "$check" = 1 ]; then
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    build "$tmp"
    if cmp -s "$tmp" "$OUTPUT"; then
        echo "app/static/css/tailwind.css is up to date."
        exit 0
    fi
    echo "app/static/css/tailwind.css is out of date: a template uses classes it does not have (or no longer uses some)." >&2
    echo "Run: bash scripts/build_css.sh   then commit app/static/css/tailwind.css" >&2
    exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"
# Into a temporary file first: a failed build leaves the committed stylesheet
# as it was, never half written.
build "$OUTPUT.tmp"
mv "$OUTPUT.tmp" "$OUTPUT"
echo "Built app/static/css/tailwind.css ($(wc -c < "$OUTPUT" | tr -d ' ') bytes)."
echo "Commit it together with the template change."
