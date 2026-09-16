@echo off
rem Build a standalone findex.exe that runs without Python: dist\findex.
rem The normal folder + findex-gui.bat setup is unchanged by this.
rem
rem   build-exe.bat            app folder: dist\findex\findex.exe  (default)
rem   build-exe.bat onefile    a single dist\findex.exe
rem
rem Copy dist\findex anywhere and double-click findex.exe. It keeps its
rem index and settings beside itself, wherever the folder is.
rem
rem Known limit: this exe is unsigned, and a managed PC can refuse to run
rem an unsigned, never-before-seen program - on one such laptop Windows
rem quarantined it and removed the folder, reported as "Windows cannot
rem access the specified device, path, or file". That is that machine's
rem policy, not a fault in the build, and there is no way round it without
rem admin rights or a code-signing certificate. On an ordinary PC it runs.
rem
rem The noisy output of pip and PyInstaller goes to build-win-log.txt so this
rem window stays readable; if anything fails, the tail of that log is shown
rem here. The finished exe is tested before this script claims success.

setlocal
cd /d "%~dp0"
set "HERE=%~dp0"
set "VENV=%HERE%.venv-build"
set "PY=%VENV%\Scripts\python.exe"
set "LOG=%HERE%build-win-log.txt"
set "MISSING="

set "MODE=--onedir"
set "APPDIR=%HERE%dist\findex"
if /i "%~1"=="onefile" set "MODE=--onefile"
if /i "%~1"=="onefile" set "APPDIR=%HERE%dist"
set "EXE=%APPDIR%\findex.exe"

echo findex standalone build> "%LOG%"
echo %DATE% %TIME%>> "%LOG%"
echo mode %MODE%>> "%LOG%"
echo.
echo Building findex.exe. This takes a few minutes.
echo Detail goes to build-win-log.txt.

rem ---------------------------------------------------------------------
rem 1. A Python to build with - installed automatically if the PC has none.
rem ---------------------------------------------------------------------
echo.
echo == Python
if exist "%PY%" goto :haveenv

set "SYSPY="
for /f "delims=" %%p in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%ensure_python.ps1" 2^>nul') do set "SYSPY=%%p"
if not defined SYSPY goto :nopython
if not exist "%SYSPY%" goto :nopython
echo    Using %SYSPY%
"%SYSPY%" -m venv "%VENV%" >> "%LOG%" 2>&1
if not exist "%PY%" (
    echo    ERROR: could not create the build environment.
    goto :failed
)

:haveenv
echo    Installing PyInstaller...
"%PY%" -m pip install --upgrade pip --quiet >> "%LOG%" 2>&1
"%PY%" -m pip install --upgrade --only-binary :all: pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 (
    echo    ERROR: could not install PyInstaller.
    goto :failed
)

rem ---------------------------------------------------------------------
rem 2. Everything the exe should carry with it.
rem
rem One pip call per package, each with a lower bound. Asked for all of
rem them at once with no floors, pip walks backwards through dozens of
rem extract-msg releases re-checking every other package against each, and
rem on Python 3.14 gives up with "resolution-too-deep" having installed
rem NOTHING - which is how a build once produced an exe with no PDF, tag,
rem .msg or live-update support and said nothing about it.
rem
rem --only-binary :all: everywhere: a missing wheel then fails in seconds
rem instead of trying to compile from source and hunting for Visual Studio.
rem extract-msg is the one exception - it needs red-black-tree-mod, which is
rem published as a source tarball only, so sdists are allowed for that one
rem package. It is pure Python, so there is still nothing to compile.
rem (vendor\ has these too, but a frozen build does not look there.)
rem ---------------------------------------------------------------------
echo.
echo == Components to bake in
call :pipget "pymupdf>=1.26" "PDF text"
call :pipget "mutagen>=1.47" "media tags"
call :pipget "watchdog>=6.0" "live updates"
call :pipget "psutil>=6.0"   "resource monitor"

echo    Outlook messages...
"%PY%" -m pip install --only-binary :all: --no-binary red-black-tree-mod "extract-msg>=0.54" >> "%LOG%" 2>&1
if errorlevel 1 set "MISSING=%MISSING% Outlook-messages"

echo    Windows built-in OCR...
"%PY%" -m pip install --only-binary :all: winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization winrt-Windows.Graphics.Imaging winrt-Windows.Media.Ocr winrt-Windows.Storage.Streams >> "%LOG%" 2>&1
if errorlevel 1 set "MISSING=%MISSING% Windows-OCR"

if not defined MISSING goto :allthere
echo.
echo    NOTE: these could not be installed and will be absent from the exe:
echo      %MISSING%
echo    The reason is in build-win-log.txt. The build continues; those file
echo    types are recorded by name only.
:allthere

set "COLLECT="
"%PY%" -c "import winrt" >nul 2>&1 && set "COLLECT=%COLLECT% --collect-submodules winrt"
"%PY%" -c "import extract_msg" >nul 2>&1 && set "COLLECT=%COLLECT% --collect-all extract_msg"
"%PY%" -c "import pymupdf" >nul 2>&1 && set "COLLECT=%COLLECT% --collect-all pymupdf"
"%PY%" -c "import mutagen" >nul 2>&1 && set "COLLECT=%COLLECT% --collect-submodules mutagen"
"%PY%" -c "import watchdog" >nul 2>&1 && set "COLLECT=%COLLECT% --collect-submodules watchdog"

rem ---------------------------------------------------------------------
rem 3. Build
rem ---------------------------------------------------------------------
echo.
echo == Building ^(a few minutes^)
if exist "%HERE%build" rd /s /q "%HERE%build" >nul 2>&1
if exist "%HERE%dist" rd /s /q "%HERE%dist" >nul 2>&1
if exist "%HERE%findex.spec" del /q "%HERE%findex.spec"
ping -n 2 127.0.0.1 >nul 2>&1

"%PY%" -m PyInstaller --noconfirm --clean %MODE% --windowed --name findex ^
    %COLLECT% ^
    "%HERE%findex_app.py" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo    Build failed.
    goto :failed
)
if not exist "%EXE%" (
    echo    The build finished but %EXE% is not there.
    goto :failed
)

rem ---------------------------------------------------------------------
rem 4. Prove it runs before saying it works.
rem
rem A windowed exe has no console of its own, so the self-test writes its
rem report to a file beside the exe and that file is shown here. Anything
rem Windows itself says - "Access is denied" and friends - lands in the log.
rem ---------------------------------------------------------------------
echo.
echo == Testing the exe
if exist "%APPDIR%\findex-selftest.txt" del /q "%APPDIR%\findex-selftest.txt"
echo ---- selftest ---->> "%LOG%"
"%EXE%" selftest >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if exist "%APPDIR%\findex-selftest.txt" goto :gotreport
echo    The exe did not produce a self-test report - it could not start.
goto :diagnose

:gotreport
type "%APPDIR%\findex-selftest.txt"
type "%APPDIR%\findex-selftest.txt" >> "%LOG%"
del /q "%APPDIR%\findex-selftest.txt" >nul 2>&1
if not "%RC%"=="0" goto :selftestbad

echo.
echo Done: %EXE%
echo.
echo   Copy the folder anywhere and double-click findex.exe.
echo   It keeps its index in the folder, wherever the folder is.
echo.
echo Log: %LOG%
pause
exit /b 0

rem ---------------------------------------------------------------------
rem Helpers
rem ---------------------------------------------------------------------
:pipget
rem %1 = pip requirement, %2 = label
echo    %~2...
"%PY%" -m pip install --only-binary :all: "%~1" >> "%LOG%" 2>&1
if errorlevel 1 set "MISSING=%MISSING% %~2"
exit /b 0

:selftestbad
echo.
echo The exe runs, but the self-test above found problems with it.
echo Send build-win-log.txt if you want it looked at.
echo.
echo Log: %LOG%
pause
exit /b 1

:diagnose
echo.
if not exist "%EXE%" (
    echo    The exe is no longer there: anti-virus has quarantined it.
    echo    That is this PC's policy - see the note at the top of this
    echo    script. Check Windows Security, Protection history.
    goto :diagnosed
)
echo    Windows would not run it. Usually Defender or a security policy
echo    refusing an unsigned program - Windows Security, Protection history
echo    will name it. See the note at the top of this script.
if /i "%~1"=="onefile" echo    A onefile exe also unpacks to %%TEMP%% on launch; try the default
if /i "%~1"=="onefile" echo    app-folder build, which does not.
:diagnosed
echo.
echo    Anything Windows said is at the end of build-win-log.txt.
goto :failed

:nopython
echo    ERROR: Python 3.9+ is needed to BUILD the exe ^(not to run it^), and
echo    it could not be installed automatically.
echo    Install it from https://www.python.org/downloads/windows/ - choose
echo    "Install for me only" so it needs no admin rights, and tick
echo    "Add python.exe to PATH". Then run this again.
goto :failed

:failed
echo.
echo ---- last 40 lines of the log ----
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 40" 2>nul
echo ----------------------------------
echo Full log: %LOG%
echo.
pause
exit /b 1
