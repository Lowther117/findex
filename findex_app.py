#!/usr/bin/env python3
"""Entry point for the OPTIONAL standalone build of findex (see
build-exe.bat / build-app.command). The normal way to run findex is
findex-gui.bat / findex-gui.command - this file changes nothing about that.

    findex.exe             opens the desktop app
    findex.exe engine ...  runs the indexing/search engine (used internally)
"""

import sys
from multiprocessing import freeze_support


def main():
    freeze_support()    # lets worker processes start inside a frozen build
    if len(sys.argv) > 1 and sys.argv[1] == "engine":
        import findex
        return findex.main(sys.argv[2:])
    import findex_gui
    return findex_gui.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
