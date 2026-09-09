#!/bin/bash
# OPTIONAL: build a standalone findex.app that runs without Python.
# The normal folder + findex-gui.command setup is unchanged by this.
#
# Everything this prints is also written to build-mac-log.txt, and the built
# app is tested before this script claims success.

cd "$(dirname "$0")" || exit 1
LOG="build-mac-log.txt"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

VENV=".venv-build-mac"
PY="$VENV/bin/python"
APP="dist/findex.app"
BIN="$APP/Contents/MacOS/findex"

say() { printf '\n== %s\n' "$1"; }
fail() { printf '\nBuild stopped: %s\nFull log: %s/%s\n' "$1" "$PWD" "$LOG"; exit 1; }

printf 'findex standalone build - %s\n' "$(date)"
printf 'macOS %s on %s\n' "$(sw_vers -productVersion 2>/dev/null)" "$(uname -m)"

# --------------------------------------------------------------------------
# Homebrew - where a bundle-able Python and any external tools come from.
#
# A double-clicked .command starts with a bare PATH, so Homebrew's folders are
# added by hand, and Homebrew itself is installed if the Mac has none (its
# installer asks for the Mac password once, in this window).
# --------------------------------------------------------------------------
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

ensure_brew() {
    command -v brew >/dev/null 2>&1 && return 0
    say "Installing Homebrew"
    echo "   This asks for your Mac password once, then takes a few minutes."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    command -v brew >/dev/null 2>&1
}

# brew_install <formula>... - installs each one (a formula that is already
# there is a no-op). Non-zero if Homebrew is unavailable or an install failed.
brew_install() {
    ensure_brew || { echo "   Homebrew is not available, so $* cannot be installed automatically."; return 1; }
    local f rc=0
    for f in "$@"; do
        echo "   brew install $f"
        HOMEBREW_NO_AUTO_UPDATE=1 brew install "$f" < /dev/null || rc=1
    done
    return $rc
}

# --------------------------------------------------------------------------
# 1. Pick an interpreter that can actually be bundled.
#
# Apple's /usr/bin/python3 is tied to the system Tcl/Tk 8.5 frameworks, which
# do not survive being copied into an app bundle - the build succeeds and the
# app then dies on launch with no window and no message. So a Python with its
# own Tk 8.6 is required, and the system one is refused by name.
# --------------------------------------------------------------------------
say "Choosing a Python to build with"

tk_version() {   # prints e.g. 8.6, or nothing if tkinter is unusable
    "$1" -c 'import tkinter;print(tkinter.TkVersion)' 2>/dev/null
}

CANDIDATES=()
[ -n "$FINDEX_BUILD_PYTHON" ] && CANDIDATES+=("$FINDEX_BUILD_PYTHON")
for v in 3.14 3.13 3.12 3.11 3.10 3.9; do
    CANDIDATES+=("/opt/homebrew/bin/python$v" "/usr/local/bin/python$v" \
                 "/Library/Frameworks/Python.framework/Versions/$v/bin/python3")
done
CANDIDATES+=("$(command -v python3 2>/dev/null)")

pick_python() {
CHOSEN=""
for c in "${CANDIDATES[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    # readlink, not python: on a fresh Mac the only "python3" is Apple's stub,
    # and merely running it pops up the Xcode command-line-tools installer.
    real="$(readlink -f "$c" 2>/dev/null || echo "$c")"
    case "$real" in
        /usr/bin/python3|/Library/Developer/CommandLineTools/*|/Applications/Xcode.app/*)
            printf '   skipping %s - Apple system Python, its Tk cannot be bundled\n' "$c"
            continue ;;
    esac
    tkv="$(tk_version "$c")"
    if [ -z "$tkv" ]; then
        printf '   skipping %s - no working tkinter\n' "$c"
        continue
    fi
    case "$tkv" in
        8.6|8.7|9.*) CHOSEN="$c"; printf '   using %s (Tk %s)\n' "$c" "$tkv"; break ;;
        *) printf '   skipping %s - Tk %s is too old to bundle\n' "$c" "$tkv" ;;
    esac
done
}
pick_python

if [ -z "$CHOSEN" ]; then
    echo "   None of the Pythons here can be bundled - adding one with Homebrew."
    echo "   (a Python with its own Tk 8.6; Apple's own /usr/bin/python3 does not qualify.)"
    if brew_install python python-tk; then
        pick_python
    fi
fi

if [ -z "$CHOSEN" ]; then
    cat <<'MSG'

None of the Pythons on this Mac can be used to build the app.

The app needs a Python that carries its own Tk 8.6. The one Apple ships
(/usr/bin/python3) uses the system Tk 8.5, which cannot be copied into an
app bundle - that is why a build can finish and the app still not open.

Install one of these, then run this again:

    brew install python python-tk          (Homebrew - simplest)
    https://www.python.org/downloads/macos/ (official installer)

If you already have one somewhere unusual, point this script at it:

    FINDEX_BUILD_PYTHON=/path/to/python3 ./build-app.command

Nothing else on this Mac is affected - findex-gui.command keeps working
exactly as before whether or not you ever build the app.
MSG
    fail "no suitable Python found"
fi

# --------------------------------------------------------------------------
# 2. Build environment
# --------------------------------------------------------------------------
say "Build environment"
if [ ! -x "$PY" ]; then
    "$CHOSEN" -m venv "$VENV" || fail "could not create the build environment"
else
    # a venv built by a different (or moved) Python is worse than none
    if ! "$PY" -c 'import sys' >/dev/null 2>&1; then
        rm -rf "$VENV"
        "$CHOSEN" -m venv "$VENV" || fail "could not recreate the build environment"
    fi
fi
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install --upgrade --only-binary :all: pyinstaller \
    || fail "could not install PyInstaller"

say "Components to bake in"
"$PY" -m pip install --only-binary :all: \
        pymupdf mutagen extract-msg watchdog pyobjc-framework-Vision \
    || echo "   WARNING: some components missing - the app builds without them."

# --------------------------------------------------------------------------
# 3. Build
# --------------------------------------------------------------------------
say "Building"
rm -rf build dist findex.spec
"$PY" -m PyInstaller --noconfirm --clean --windowed --name findex \
    --osx-bundle-identifier com.lowther.findex \
    --collect-submodules watchdog \
    --collect-submodules mutagen \
    --collect-all extract_msg \
    --collect-all pymupdf \
    --hidden-import objc --hidden-import Foundation \
    --hidden-import Quartz --hidden-import Vision \
    findex_app.py \
    || fail "PyInstaller failed - the messages above say why"

[ -x "$BIN" ] || fail "the build finished but $APP is not there"

# --------------------------------------------------------------------------
# 4. Make it launchable
#
# An ad-hoc signature is what lets a locally built app open at all on Apple
# silicon, and stray extended attributes invalidate it.
# --------------------------------------------------------------------------
say "Signing"
xattr -cr "$APP" 2>/dev/null || true
if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign - --timestamp=none "$APP" \
        && codesign --verify --deep --strict "$APP" \
        && echo "   ad-hoc signature ok" \
        || echo "   WARNING: signing did not complete - the app may be blocked on first open"
else
    echo "   codesign not available (install the Xcode command line tools)"
fi

# --------------------------------------------------------------------------
# 5. Prove it runs before saying it works
# --------------------------------------------------------------------------
say "Testing the built app"
if "$BIN" selftest; then
    RESULT=ok
else
    RESULT=problems
fi

echo
if [ "$RESULT" = ok ]; then
    cat <<MSG
Done: $PWD/$APP

Move it wherever you like. It keeps its index and settings in the folder
the app sits in - not inside the app - so put it in its own folder rather
than loose in Applications alongside everything else.
MSG
else
    cat <<MSG
The app was built but the self-test above found problems, so it may not
open properly. The whole run is in $PWD/$LOG.
MSG
fi
echo "Log: $PWD/$LOG"
