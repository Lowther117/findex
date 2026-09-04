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

```
Findex/
  findex.py           engine + CLI
  findex_gui.py       Tkinter desktop app
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
  install. Tesseract still works as a fallback on systems without one: the
  app offers to install it and resumes the index run by itself afterwards.

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
Help > Search syntax has the query cheat-sheet. Light and dark mode follow
your system setting; the Appearance menu switches them manually.

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
- The *Type* dropdown lists every file type actually in your index, with
  counts - plus a `folders` entry; pick one or type a list like `pdf, docx`.
  Everything by default.
- The list works like a file manager: Ctrl/Cmd-click and Shift-click select
  several files, Ctrl/Cmd+A selects everything shown. Copy or cut the
  selection (Ctrl/Cmd+C / X) and paste it straight into Explorer or Finder -
  or paste into a folder you pick with Ctrl/Cmd+V, including files copied
  FROM Explorer/Finder. Delete sends files to the Recycle Bin / Bin after a
  confirmation (never permanent), and the list updates immediately.
- Click column headers to sort. Double-click a hit to open it; right-click for
  the full menu. The pane underneath shows the matching text with the hit
  highlighted.

**Index tab**

- Add the folders or drives you want covered - or hit **Add all drives** to
  list every internal and removable drive on the computer in one click
  (network drives stay out unless you add them yourself) - then
  **Start indexing**. Progress updates live and **Stop** always works —
  indexing runs as a separate process, so the window never freezes.
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
- *Optimise + compact* merges the FTS index and vacuums the database.
- *Clear index...* deletes findex's database and starts fresh - after a
  confirmation, and never touching the files on your disk. Also available as
  `findex clear` on the command line.

## CLI

```
findex index D:\ E:\Documents          build or update the index
findex watch D:\                       live updates until stopped (Ctrl+C)
findex find "C: content:dan ext:pdf"   Everything-style search
findex find "budget !draft folder:"
findex search "quarterly AND revenue"  content search (raw FTS5)
findex name "*.mp4" -n 100             filename search - any file type
findex dupes                           duplicate files (same name + size)
findex stats                           what is indexed
findex vacuum                          optimise and compact
findex clear                           delete the index, start fresh
findex gui                             open the desktop app
```

`--db PATH` puts the index somewhere other than next to the script.

## What gets recorded

- **Names**: every file AND folder, whatever the type. OneDrive online-only
  placeholders are included (their names and sizes are known without
  downloading anything). An index built by an older findex gains folder
  support automatically - the next indexing run fills the folders in.
- **Contents**: extracted from
  - `.pdf` (PyMuPDF) - with optional OCR of scans, see below
  - `.docx/.docm`, `.xlsx/.xlsm`, `.pptx/.pptm`, `.rtf`
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
that changes. But if you want to hand someone a single file that needs no
Python at all, run **build-exe.bat** (Windows) or **build-app.command** (Mac):
it produces `dist/findex.exe` / `dist/findex.app` with every component baked
in. Building needs Python once; the result runs anywhere and keeps its index
next to itself, so it is just as portable as the folder. Build output stays
out of the repo.

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
