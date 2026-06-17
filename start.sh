#!/usr/bin/env bash
# ZoomCast launcher — creates venv, installs deps, runs controller

set -e
cd "$(dirname "$0")"

VENV=".venv"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║              ZoomCast — Launcher                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Python check ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "❌  python3 not found. Install Python 3.10+ from https://python.org"
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "🐍  Python $PY_VER detected"

# ── Virtual environment ────────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "📦  Creating virtual environment…"
    python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "📥  Installing / verifying dependencies…"
pip install --quiet --upgrade pip
pip install --quiet obsws-python pynput pyobjc-framework-Cocoa pyobjc-framework-Quartz

echo "✅  Dependencies ready."
echo ""
echo "Starting ZoomCast controller…"
echo "  → Make sure OBS is running with WebSocket enabled (Tools → WebSocket Server Settings)"
echo "  → Grant Accessibility access to Terminal in System Settings → Privacy → Accessibility"
echo ""

# ── Run ───────────────────────────────────────────────────────────────────────
python3 zoom_controller.py "$@"
