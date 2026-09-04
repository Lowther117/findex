#!/bin/bash
# OPTIONAL: build a standalone findex.app that runs without Python.
# The normal folder + findex-gui.command setup is unchanged by this.
cd "$(dirname "$0")" || exit 1
VENV=".venv-build-mac"
PY="$VENV/bin/python"
[ -x "$PY" ] || python3 -m venv "$VENV" || { echo "Python 3.9+ needed to build"; exit 1; }
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install --only-binary :all: pyinstaller || { echo "could not install PyInstaller"; exit 1; }
"$PY" -m pip install --only-binary :all: pymupdf mutagen extract-msg pyobjc-framework-Vision watchdog \
    || echo "WARNING: some components missing - app builds without them."
"$PY" -m PyInstaller --noconfirm --clean --windowed --name findex --collect-submodules watchdog findex_app.py \
    || { echo "Build failed."; exit 1; }
echo "Done: dist/findex.app - copy it anywhere; it keeps its index next to itself."
