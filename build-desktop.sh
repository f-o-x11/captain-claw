#!/usr/bin/env bash
#
# Build Captain Claw Desktop App (Electron + PyInstaller backend).
#
# Usage:
#   ./build-desktop.sh                 # build for current platform
#   ./build-desktop.sh --platform mac  # explicit platform (mac|win|linux)
#   ./build-desktop.sh --backend-only  # only build the Python backend
#   ./build-desktop.sh --electron-only # only package Electron (assumes backend already built)
#   ./build-desktop.sh --clean         # wipe build artifacts first
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse flags ──────────────────────────────────────────────────
CLEAN=false
BACKEND_ONLY=false
ELECTRON_ONLY=false
PLATFORM=""

for arg in "$@"; do
    case "$arg" in
        --clean)          CLEAN=true ;;
        --backend-only)   BACKEND_ONLY=true ;;
        --electron-only)  ELECTRON_ONLY=true ;;
        --platform)       :;; # value follows
        mac|win|linux)    PLATFORM="$arg" ;;
        -h|--help)
            echo "Usage: $0 [--clean] [--backend-only] [--electron-only] [--platform mac|win|linux]"
            exit 0
            ;;
    esac
done

# Auto-detect platform if not specified
if [[ -z "$PLATFORM" ]]; then
    case "$(uname -s)" in
        Darwin*)  PLATFORM="mac" ;;
        Linux*)   PLATFORM="linux" ;;
        MINGW*|MSYS*|CYGWIN*)  PLATFORM="win" ;;
        *)        echo "Unknown platform: $(uname -s)"; exit 1 ;;
    esac
fi

echo "========================================="
echo "  Captain Claw Desktop Build"
echo "  Platform: $PLATFORM"
echo "========================================="
echo ""

# ── Clean ────────────────────────────────────────────────────────
if $CLEAN; then
    echo "Cleaning build artifacts..."
    rm -rf build/ dist/captain-claw/ dist/desktop/
    rm -rf desktop/node_modules/
    echo ""
fi

if ! $ELECTRON_ONLY; then
    # ── Step 1: Build Flight Deck frontend ───────────────────────
    # Before the backend: PyInstaller copies captain_claw/flight_deck/static
    # into the binaries, so a frontend built after it never reaches the app.
    echo "Step 1: Building Flight Deck frontend..."
    echo "─────────────────────────────────"

    cd flight-deck

    if ! command -v npm &>/dev/null; then
        echo "Error: npm is required but not found."
        echo "Install Node.js from https://nodejs.org"
        exit 1
    fi

    npm install
    npm run build

    cd "$SCRIPT_DIR"

    echo ""
    echo "Flight Deck frontend build complete."
    echo ""

    # ── Step 1b: Build Python backend with PyInstaller ───────────
    echo "Step 1b: Building Python backend..."
    echo "─────────────────────────────────"

    ./build.sh

    echo ""
    echo "Backend build complete."
    echo ""

    if $BACKEND_ONLY; then
        echo "Backend-only build done. Binaries in dist/captain-claw/"
        exit 0
    fi
fi

# ── Step 2: Install Electron dependencies ────────────────────────
echo "Step 2: Installing Electron dependencies..."
echo "─────────────────────────────────"

cd desktop

if ! command -v npm &>/dev/null; then
    echo "Error: npm is required but not found."
    echo "Install Node.js from https://nodejs.org"
    exit 1
fi

npm install

echo ""

# ── Step 3: Verify backend binaries exist ────────────────────────
echo "Step 3: Verifying backend binaries..."
echo "─────────────────────────────────"

BACKEND_DIR="$SCRIPT_DIR/dist/captain-claw"

if [[ ! -d "$BACKEND_DIR" ]]; then
    echo "Error: Backend build not found at $BACKEND_DIR"
    echo "Run without --electron-only first, or run ./build.sh separately."
    exit 1
fi

EXE_NAME="captain-claw-web"
FD_EXE_NAME="captain-claw-fd"
if [[ "$PLATFORM" == "win" ]]; then
    EXE_NAME="captain-claw-web.exe"
    FD_EXE_NAME="captain-claw-fd.exe"
fi

if [[ ! -f "$BACKEND_DIR/$EXE_NAME" ]]; then
    echo "Error: $EXE_NAME not found in $BACKEND_DIR"
    exit 1
fi

if [[ ! -f "$BACKEND_DIR/$FD_EXE_NAME" ]]; then
    echo "Error: $FD_EXE_NAME not found in $BACKEND_DIR"
    exit 1
fi

# The Electron shell must match the backend's CPU: PyInstaller builds for
# this machine only, so an x64 shell around arm64 binaries runs under
# Rosetta on Apple silicon and can't start its backend on an Intel Mac.
if [[ "$PLATFORM" == "mac" ]] && command -v lipo &>/dev/null; then
    BACKEND_ARCH="$(lipo -archs "$BACKEND_DIR/$FD_EXE_NAME")"
else
    BACKEND_ARCH="$(uname -m)"
fi
case "$BACKEND_ARCH" in
    arm64|aarch64) ARCH_FLAG="--arm64" ;;
    x86_64|amd64)  ARCH_FLAG="--x64" ;;
    *)
        echo "Error: unsupported backend architecture: $BACKEND_ARCH"
        exit 1
        ;;
esac

echo "  Backend binary: $BACKEND_DIR/$EXE_NAME"
echo "  Flight Deck binary: $BACKEND_DIR/$FD_EXE_NAME"
echo "  Architecture: $BACKEND_ARCH"
echo ""

# ── Step 4: Package with electron-builder ────────────────────────
echo "Step 4: Packaging Electron app..."
echo "─────────────────────────────────"

# electron-builder never empties its output folder: installers from an
# earlier build (another arch, an older backend) would sit next to the new ones.
# An open Finder window can re-create .DS_Store mid-delete; the folder staying
# behind with only that in it is harmless.
rm -rf "$SCRIPT_DIR/dist/desktop" 2>/dev/null || true

case "$PLATFORM" in
    mac)    npm run dist:mac -- "$ARCH_FLAG" ;;
    win)    npm run dist:win -- "$ARCH_FLAG" ;;
    linux)  npm run dist:linux -- "$ARCH_FLAG" ;;
esac

cd "$SCRIPT_DIR"

echo ""
echo "========================================="
echo "  Build Complete!"
echo "========================================="
echo ""
echo "  Output: dist/desktop/"
echo ""

# List output files
if [[ -d "dist/desktop" ]]; then
    echo "  Artifacts:"
    find dist/desktop -maxdepth 1 \( -name "*.dmg" -o -name "*.zip" -o -name "*.exe" -o -name "*.AppImage" -o -name "*.deb" -o -name "*.tar.gz" \) -exec echo "    {}" \;
fi
