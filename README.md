# findex

Local filename **and** file-contents search for Windows (runs on macOS/Linux too).
SQLite FTS5 index kept on disk, not in RAM.

**Every file and folder** under the indexed roots is recorded by name, size
and date — photos, music, video, executables, the folders they sit in, the
lot — so name search covers the whole drive, like Everything does. On top of
that, text is extracted from document types (PDF, Word, Excel, PowerPoint,
plain text and code) for full-content search, and one Everything-style search
box drives it all: `C: content:dan ext:pdf !draft`.

Fully portable: the folder is the app. Move it, rename it, copy it to the other
PC or run it off a USB stick — nothing outside the folder is read or written,
and no machine-specific path is ever stored.

## Which file do I use?

| File | Windows | Mac | What it is |
|---|---|---|---|
| `findex-gui.bat` | **double-click this** | - | Opens the desktop app. First run builds `.venv-win` and installs PyMuPDF. |
| `findex-gui.command` | - | **double-click this** | Opens the desktop app. First run builds `.venv-mac` (or `.venv-linux` on Linux) and installs PyMuPDF. |
| `run.bat` | same as above | - | The house-standard name: just calls `findex-gui.bat`. |
| `run.command` | - | same as above | The house-standard name: just calls `findex-gui.command`. |
| `findex.bat` | command line | - | The CLI: `findex index D:\`, `findex find "..."`, `findex stats`. Same environment as the app. |
| `findex.py` | engine | engine | The indexer and search engine, and the CLI itself (`python findex.py ...`). The app runs this in the background. |
| `findex_gui.py` | the app | the app | The Tkinter window. The launchers run it; `python findex_gui.py` works too. |
| `findex_app.py` | build only | build only | Entry point compiled into the standalone exe/app. Not run directly. |
| `theme.py` | the app | the app | The shared light/dark palette and ttk styling the window uses. Not run directly. |
| `build-exe.bat` | optional | - | Builds `dist\findex\findex.exe` - a standalone app folder that needs no Python. Only if you want findex as an ordinary app. |
| `build-app.command` | - | optional | Builds `dist/findex.app` - the same thing for the Mac. |
| `ensure_python.ps1` | helper | - | Used by `build-exe.bat` to find (or install) a real Python. Not run directly. |
| `vendor/` | needed | needed | Bundled pure-Python libraries (tags, .msg files). Part of the repo - leave it alone. |

Everything else in the folder (`findex.db`, `findex_gui.json`, `.venv-*`,
`dist/`, `build/`) is created by the files above. Nothing there is precious:
delete `.venv-*` and the launcher rebuilds it, delete `findex.db` and the next
indexing run starts from scratch.

```
Findex/
  findex.py           engine + CLI
  findex_gui.py       Tkinter desktop app
  theme.py            light/dark palette + styling for the app
  findex_app.py       entry point for the optional standalone build
  findex.bat          CLI launcher (Windows)
  findex-gui.bat      GUI launcher (Windows) - double-click this
  findex-gui.command  GUI launcher (macOS/Linux) - double-click this
  findex.db           the index          } created on first run,
  findex_gui.json     settings           } always beside these scripts
  vendor/             bundled pure-Python libraries (part of the repo)
  .venv-win           Windows environment  } built automatically,
  .venv-mac           macOS environment    } one per platform
```

## First run

Double-click **findex-gui.bat** (Windows) or **findex-gui.command** (Mac).
The launcher builds a local environment inside the folder and opens the app.

Python 3.9+ must be on the machine; everything else takes care of itself:

- Most components are **built in**: the pure-Python libraries for music/video
  tags and Outlook .msg files ship inside the folder (`vendor/`), so a fresh
  clone can use them immediately - no installs, no network.
- PyMuPDF (PDF text) and watchdog (live updates) are compiled per platform,
  so they cannot be bundled; the app installs them into its own environment
  automatically on launch when missing, with progress in the Output pane.
- OCR uses the engine **built into Windows and macOS** - nothing extra to
  install (the small Python bridge to it, `winrt-*` / `pyobjc-framework-Vision`,
  is installed by the app automatically like PyMuPDF). Tesseract still works
  as a fallback on systems without one - Linux, say: the app offers to
  install it on Windows/macOS and resumes the index run by itself afterwards;
  on Linux install it with your package manager.

Nothing is installed system-wide except that optional tesseract fallback.

## Portable by design

- The index and the settings always sit next to the scripts. Paths inside the
  folder are stored **relative**, so moving or renaming the folder changes
  nothing; an absolute path left over from somewhere else is ignored if it no
  longer exists.
- Each platform gets its own environment (`.venv-win`, `.venv-mac`), so the same
  folder works on the Windows PC and the MacBook without either clobbering the
  other. A virtual environment broken by a move is detected and rebuilt.
- Indexing only prunes entries **under the folders it just scanned**. Index the
  Documents folder today and the D: drive tomorrow and both stay searchable;
  run it on a machine where an external drive is missing and that drive's
  entries are left untouched. The app warns before indexing if a listed folder
  is not currently present.
- Launchers resolve their own location, so shortcuts and symlinks are fine.

## Incremental by default

The index is a file (`findex.db`) that lives beside the scripts and persists
between runs. Indexing again only touches files that are **new, or whose size
or timestamp changed**; everything else is counted as *unchanged* and skipped,
so a repeat run over an unchanged drive takes seconds rather than hours.
Deleted files are dropped from the index as they are noticed.

The Index tab shows the counts live while it runs, and underneath the
statistics line tells you when the index was last updated and what that run
did. *Re-extract everything* is the only thing that forces a full re-read; it
asks for confirmation and is never remembered between sessions.

## The app

Every control describes itself: hover over it and the description appears
immediately as grey text in the status bar, with a balloon after a short pause.
Help > Search syntax has the query cheat-sheet. The app opens in dark mode;
Appearance > Dark mode (or Ctrl+D) switches to light and back, and the choice
is remembered in `findex_gui.json`.

**Search tab**

- The list starts full: your indexed files and folders, newest first, with
  the status bar showing the true total. Typing narrows it live; clearing
  the box brings the full list back.
- **One box, Everything-style.** Bare words match names as you type -
  instantly, backed by a trigram index - with `*`/`?` wildcards. Add filters
  in any order and combine them freely:
  - `content:word` searches the text inside files (live, prefix-matching as
    you type; `content:"exact phrase"` and FTS5 syntax like `content:budg*`
    work; half-typed queries quietly fall back to a literal word search)
  - `C:` or `D:\Photos` or `/Users/dan` limits results to that drive/folder
  - `ext:pdf;docx` limits the type; `folder:` / `file:` limit the kind
  - `!anything` leaves results out: `!draft`, `!ext:tmp`, `!C:\Windows`
  - e.g. `C: content:dan ext:pdf !draft`
- **Weighted results**: name matches come back exact-name first, then names
  starting with the term, then newest; content matches are relevance-ranked
  (bm25). Browsing with no terms is newest-first.
- **Duplicates** lists files sharing the same name AND size, grouped with
  the biggest first, plus a total of the space you would get back keeping
  one copy of each - delete the extras straight from the list (Recycle
  Bin / Bin, as always). The Type box narrows it.
- The *Type* dropdown sits right of the search bar. **Groups** come first -
  Images, Videos, Audio, Documents, Compressed, Code, Programs, Emails - each
  covering its whole family of extensions in one pick, then every file type
  actually in your index individually, all with live counts, plus a `folders`
  entry. Type your own list too (`pdf, docx`, or mix in a group: `pdf,
  images`). The list rebuilds itself every time it opens, so it is never
  stale or empty. Everything by default.
- The list works like a file manager: Ctrl/Cmd-click and Shift-click select
  several files, Ctrl/Cmd+A selects everything shown. Copy or cut the
  selection (Ctrl/Cmd+C / X) and paste it straight into Explorer or Finder -
  or paste into a folder you pick with Ctrl/Cmd+V, including files copied
  FROM Explorer/Finder. That picker opens in your Downloads folder unless
  you set another under File > Default save folder... (Reset to Downloads
  undoes it; the choice lives in `findex_gui.json`). Delete sends files to the Recycle Bin / Bin after a
  confirmation (never permanent), and the list updates immediately.
- Click column headers to sort. Double-click a hit to open it; right-click for
  the full menu. The pane underneath shows the matching text with the hit
  highlighted.

**Index tab**

- Add the folders or drives you want covered - or hit **Add all drives** to
  list every internal and removable drive on the computer in one click
  (network drives stay out unless you add them yourself) - then
  **Start indexing**. The list is saved in the index database itself, so it
  is there next time and goes wherever the database goes. Progress updates
  live and **Stop** always works — indexing runs as a separate process, so
  the window never freezes.
- *Re-extract everything* forces a full rebuild; normally findex only touches
  files whose size or timestamp changed, which is why repeat runs are quick.
- *Auto re-index every N minutes* re-runs the same folders on a timer while the
  app is open.
- *Live updates* watches the listed folders while the app is open and folds
  changes into the index **within seconds** - new, modified, renamed and
  deleted files and folders, with text extraction included - so search stays
  current without waiting for the next run. Runs quietly alongside normal
  indexing and is shut down with the app. (Uses the OS's own change
  notifications via the `watchdog` component, installed automatically.)
- *Export file tree...* writes everything the index knows - every folder and
  file under every indexed location - to a file, straight from the database,
  so it takes seconds even for hundreds of thousands of entries and needs no
  disk walk. Save it as `.txt` for the classic tree drawing with a size and
  file-count total on every folder, `.csv` for one row per path with size and
  date, or `.json` for a nested structure. Also `findex tree` on the command
  line, with `--under FOLDER` to export one branch.
- *Optimise + compact* merges the FTS index and vacuums the database.
- *Clear index...* deletes findex's database and starts fresh - after a
  confirmation, and never touching the files on your disk. Also available as
  `findex clear` on the command line.

**Changes tab**

- A journal of every change findex has noticed inside the indexed
  locations - files and folders **added, modified, renamed or deleted** -
  with when it was seen. Filter by text, by kind of change, and by time
  (last hour, today, 7 days, 30 days, everything). Double-click opens the
  file, or its folder if it has since gone; right-click for Show in folder
  and Copy path.
- Two things write to it. An **index run** records what differs from the
  last run: a path it had not seen is *added*, a changed size or timestamp
  is *modified*, a path that has gone is *deleted*. A run cannot tell a
  rename from a delete-plus-add, and records exactly that. **Live updates**
  records changes as they happen and *does* see renames, so they appear as
  *renamed* with the old name alongside; those rows are marked *live*.
- The first run over a new location is not journaled - every file would be
  "added", which is true but tells nobody anything. Recording starts from
  the second run, or as soon as Live updates is on.
- Live-update noise is collapsed: an editor's burst of saves is one
  *modified*, a file created and deleted within the same two-second tick
  (Office lock files and the like) is dropped, and a file created then
  edited is just *added*.
- Entries are kept for 90 days by default (`findex journal --keep-days N`,
  0 = forever); older ones are pruned at the end of each index run.
  **Clear journal...** empties it; the index is untouched.

**Status strip** (along the bottom, on all tabs)

- The progress bar mirrors the one on the Index tab, so you can start a run,
  switch to Search and carry on working while it fills.
- **CPU** and **RAM** cover findex *and* every worker process it has started,
  which is where the load actually is during a run - a figure for the window
  alone would sit near zero through the very thing worth watching. CPU is a
  share of the whole machine, so 100% means every core is busy, rather than
  psutil's sum-across-cores reading of 780%. A process that has just appeared
  reads 0% until the next refresh two seconds later; that is how CPU sampling
  works, not a stall. The readout hides itself if the `psutil` component is
  missing.

## CLI

```
findex index D:\ E:\Documents          build or update the index
findex index                           ...of the remembered locations
findex roots                           show what is remembered
findex roots --add F:\ --forget E:\Documents
findex watch D:\                       live updates until stopped (Ctrl+C)
findex find "C: content:dan ext:pdf"   Everything-style search
findex find "budget !draft folder:"
findex search "quarterly AND revenue"  content search (raw FTS5)
findex name "*.mp4" -n 100             filename search - any file type
findex dupes                           duplicate files (same name + size)
findex tree                            export the index as a tree (Downloads)
findex tree -o C:\out.csv --under D:\Work
findex journal                         what changed, newest first
findex journal --since 24h --type deleted
findex journal invoice --under D:\Work -n 50
findex stats                           what is indexed
findex vacuum                          optimise and compact
findex clear                           delete the index, start fresh
findex gui                             open the desktop app
```

`--db PATH` puts the index somewhere other than next to the script.

The locations you index are remembered **in the index itself** - a `roots`
table in `findex.db` - not in a settings file beside whichever copy of findex
wrote it. So `findex index` and `findex watch` with no arguments use what was
indexed before, the desktop app's folder list is that same table, and pointing
either at an existing database brings its folder list with it. Clearing the
index keeps the list. A database built by an older findex takes the list from
the settings file the first time the app opens it, and keeps it from then on.

## What gets recorded

- **Names**: every file AND folder, whatever the type. OneDrive online-only
  placeholders are included (their names and sizes are known without
  downloading anything). An index built by an older findex gains folder
  support automatically - the next indexing run fills the folders in.
- **Contents**: extracted from
  - `.pdf` (PyMuPDF) - with optional OCR of scans, see below
  - `.docx/.docm`, `.xlsx/.xlsm` (shared and inline strings, not the
    numbers), `.pptx/.pptm`, `.rtf`
  - `.epub` ebooks and LibreOffice `.odt/.ods/.odp`
  - pre-2007 Office `.doc/.xls/.ppt` (best-effort text scrape)
  - saved emails: `.eml` built in, Outlook `.msg` with the `extract-msg` library
  - `.zip` and `.cbz` archives - the *file names inside* become searchable,
    so you can find which archive holds a file without opening anything
  - audio/video tags (artist, album, title...) for mp3, m4a, flac, ogg, mp4
    and more, with the `mutagen` library
  - plain-text and code files

  400k characters per file, 300 MB ceiling (tag reading is exempt - it only
  touches the file header). Contents of OneDrive online-only files are only
  read with *Include OneDrive online-only files* ticked, which forces
  downloads. A file indexed name-only is picked up for extraction
  automatically once it qualifies - including types that gain support later.
- **OCR** (*OCR scanned PDFs* in the app, `--ocr` on the CLI): a PDF with no
  real text layer gets its first 20 pages rendered and read. The reading is
  done by the OCR engine already built into Windows (Windows.Media.Ocr) or
  macOS (Apple Vision) - no extra programs to install. Tesseract is used as
  a fallback when neither is available. Much slower than normal indexing, so
  leave it off for huge image-heavy collections.
- **Self-contained**: tag and .msg support is bundled in `vendor/`; PyMuPDF
  is installed automatically by the app when missing. Until a component is
  available, the affected files simply stay name-only - no errors.
- **Skipped entirely**: system and machinery folders — Windows, Program Files,
  ProgramData, AppData, $Recycle.Bin, node_modules, .git, virtual environments
  and similar. Note this is by folder *name*, so a data folder that happens to
  be called e.g. `recovery` or `env` is also skipped.

## Optional: a standalone app

The normal way to run findex is this folder with its launchers - nothing about
that changes. But if you would rather have findex as an ordinary app that needs
no Python at all, run **build-exe.bat** (Windows) or **build-app.command**
(Mac). Each one builds findex with every component baked in and tests what it
built:

| | produces | run it with |
|---|---|---|
| Windows | `dist\findex` - an app folder | `findex.exe` inside it (`build-exe.bat onefile` for a single file instead) |
| Mac | `dist/findex.app` | double-click it |

Both are portable: copy the folder (or the `.app`) anywhere and it runs from
there, keeping its index beside itself - beside `findex.exe`, and beside (not
inside) `findex.app`. Nothing is installed on the machine and nothing needs
admin rights to build or run.

The scripts look after their own prerequisites. On Windows a real Python is
found, or installed per-user from python.org. On the Mac an interpreter that
can actually be bundled is chosen (Apple's `/usr/bin/python3` is refused - its
Tk cannot be copied into an app that opens) and, if there is none, Homebrew
installs one, installing Homebrew itself first if needed (that step asks for
your password once).

Each build ends by running the built app's own self-test and says what is
wrong rather than Done if something is missing; the full run is in
`build-win-log.txt` / `build-mac-log.txt`.

### The exe on a managed PC

`findex.exe` is unsigned - PyInstaller builds are - and a PC under a security
policy can refuse to run an unsigned, never-before-seen program. On one such
laptop the exe was quarantined and its folder removed, and Windows reported:

> Windows cannot access the specified device, path, or file. You may not have
> the appropriate permissions to access the item.

That is the machine's policy doing its job, not a fault in the build, and the
two ways round it - an anti-virus exclusion or a code-signing certificate -
need admin rights or money. On an ordinary PC the exe runs. If you ever need
findex on a machine like that, commit `8457fc2` in this repo's history has a
version of `build-exe.bat` that makes a folder built on python.org's own
signed embeddable Python instead of an exe.

### Where the components come from

`vendor/` carries the pure-Python ones - `mutagen` for media tags and
`extract_msg` with its whole dependency tree for Outlook messages - so a fresh
clone needs no installs for those. A frozen build does not look in `vendor/`,
so both build scripts pip-install them. That needs one exception: `extract-msg`
depends on `red-black-tree-mod`, which is published as a source tarball only,
so under `--only-binary` pip declares the whole thing impossible. Both builds
allow an sdist for that one package - it is pure Python, so nothing compiles.

Everything with compiled parts - PyMuPDF for PDF text, watchdog for live
updates, the WinRT or Vision OCR bindings - is pip-installed at build time,
**one package per call, each with a lower bound**. Asked for together with no
floors, pip walks backwards through dozens of releases re-checking every other
package against each, and on Python 3.14 gives up with `resolution-too-deep`
having installed nothing - which once produced a findex with no PDF, tag,
`.msg` or live-update support and said nothing about it. Anything that
genuinely cannot be installed is now named on screen before the build
continues.

## Long paths

Windows' 260-character path limit does not apply. Every filesystem call
findex makes - the directory walk, every extractor, the live-updates watcher -
goes through the `\\?\` long-path prefix when a path needs it, so files any
depth down are read and indexed like any other. The prefix is used only for
the call; paths are recorded and shown without it. This matters more for the
standalone exe than for running from source: Python itself declares long-path
support to Windows, but a PyInstaller bootloader does not, so without the
prefix a deep file would be silently skipped by the exe and found by the
script. (Opening or revealing such a file from the app hands the path to
Explorer, which has its own opinions about long paths.)

## Known limits

- The filename index needs three or more consecutive literal characters in a
  search to be instant; one- or two-character searches fall back to a scan
  (still fast, just not free).
- Directory walking, not MFT/USN enumeration — slower to index than Everything,
  but needs no admin rights.
- Stop (and closing the window) ends the whole worker tree immediately;
  everything indexed up to that point is kept, and the stats and list refresh
  on stop.
- The index stores absolute file paths, so results from a machine you are not
  currently on will not open until you are back on it.

## Licence

MIT No Attribution (MIT-0): do whatever you like with it - no credit needed, no warranty. See `LICENSE`.
