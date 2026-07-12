#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
BINARIES_DIR="$REPO_ROOT/src-tauri/binaries"

echo "=== MoneyMaker Sidecar Build ==="

# Detect target triple from Rust toolchain
TRIPLE=$(rustc -Vv | grep host | cut -f2 -d' ')
echo "Target triple: $TRIPLE"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

# Check/install PyInstaller
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller..."
    pip3 install pyinstaller
fi

# Install backend dependencies
echo "Installing backend dependencies..."
cd "$BACKEND_DIR"
pip3 install -r requirements.txt

# Build
echo ""
echo "Building sidecar binary..."
python3 build.py --output-dir "$BINARIES_DIR"

# Verify the binary exists with the correct triple suffix
EXPECTED="$BINARIES_DIR/moneymaker-sidecar-$TRIPLE"
if [ -f "$EXPECTED" ]; then
    chmod +x "$EXPECTED"
    echo ""
    echo "=== Build complete ==="
    ls -la "$EXPECTED"
else
    echo ""
    echo "ERROR: Expected binary not found at $EXPECTED"
    echo "Contents of binaries/:"
    ls -la "$BINARIES_DIR"/ 2>/dev/null
    exit 1
fi
