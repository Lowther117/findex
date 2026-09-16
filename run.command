#!/bin/bash
# House-standard launcher name: runs the desktop app via findex-gui.command.
exec "$(dirname "$0")/findex-gui.command" "$@"
