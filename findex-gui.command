#!/bin/bash
# findex desktop app - portable: everything it needs lives in this folder.
cd "$(dirname "$0")" || exit 1

case "$(uname -s)" in
    Darwin) VENV=".venv-mac" ;;
    *)      VENV=".venv-linux" ;;
esac
PY="$VENV/bin/python"

if [ -x "$PY" ] && ! "$PY" -c "import sys" >/dev/null 2>&1; then
    echo "Local environment is broken (folder moved?) - rebuilding..."
    rm -rf "$VENV"
fi

if [ ! -x "$PY" ]; then
    echo "First run from this location - creating a local environment..."
    python3 -m venv "$VENV" || {
        echo "ERROR: python3 not found. Install Python 3.9+ from python.org."
        read -r -p "Press Enter to close."
        exit 1
    }
    "$PY" -m pip install --upgrade pip --quiet
    "$PY" -m pip install pymupdf --quiet \
        || echo "WARNING: PyMuPDF install failed - PDFs will not be indexed."
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "ERROR: this Python has no tkinter, so the window cannot open."
    echo "Install Python from python.org - some system builds omit Tk."
    read -r -p "Press Enter to close."
    exit 1
fi

exec "$PY" findex_gui.py "$@"
