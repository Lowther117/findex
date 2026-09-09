@echo off
rem OPTIONAL: build a standalone findex.exe that runs without Python.
rem The normal folder + findex-gui.bat setup is unchanged by this.
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
set "EXE=%HERE%dist\findex.exe"

echo findex standalone build> "%LOG%"
echo %DATE% %TIME%>> "%LOG%"
echo.
echo Building the standalone findex.exe. This takes a few minutes.
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
rem 2. Everything the app should carry with it.
rem
rem --only-binary :all: everywhere: a missing wheel then fails in seconds
rem instead of trying to compile from source and hunting for Visual Studio.
rem ---------------------------------------------------------------------
echo.
echo == Components to bake in
echo    PDF text, media tags, Outlook messages, live updates...
"%PY%" -m pip install --only-binary :all: pymupdf mutagen extract-msg watchdog >> "%LOG%" 2>&1
if errorlevel 1 echo    WARNING: some components missing - the exe builds without them.

echo    Windows built-in OCR...
"%PY%" -m pip install --only-binary :all: winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization winrt-Windows.Graphics.Imaging winrt-Windows.Media.Ocr winrt-Windows.Storage.Streams >> "%LOG%" 2>&1
if errorlevel 1 echo    WARNING: Windows OCR components missing - the exe builds without OCR.

set "WINRT="
"%PY%" -c "import winrt" >nul 2>&1 && set "WINRT=--collect-submodules winrt"

rem ---------------------------------------------------------------------
rem 3. Build
rem ---------------------------------------------------------------------
echo.
echo == Building ^(a few minutes^)
if exist "%HERE%build" rd /s /q "%HERE%build"
if exist "%HERE%dist" rd /s /q "%HERE%dist"
if exist "%HERE%findex.spec" del /q "%HERE%findex.spec"

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name findex ^
    %WINRT% ^
    --collect-submodules watchdog ^
    --collect-submodules mutagen ^
    --collect-all extract_msg ^
    --collect-all pymupdf ^
    "%HERE%findex_app.py" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo    Build failed.
    goto :failed
)
if not exist "%EXE%" (
    echo    The build finished but dist\findex.exe is not there.
    goto :failed
)

rem ---------------------------------------------------------------------
rem 4. Prove it runs before saying it works.
rem
rem A windowed exe has no console of its own, so the self-test writes its
rem report to a file beside the exe and that file is shown here.
rem ---------------------------------------------------------------------
echo.
echo == Testing the built exe
if exist "%HERE%dist\findex-selftest.txt" del /q "%HERE%dist\findex-selftest.txt"
"%EXE%" selftest >nul 2>&1
set "RC=%ERRORLEVEL%"
if exist "%HERE%dist\findex-selftest.txt" (
    type "%HERE%dist\findex-selftest.txt"
    type "%HERE%dist\findex-selftest.txt" >> "%LOG%"
) else (
    echo    The exe did not produce a self-test report.
    set "RC=1"
)

echo.
if "%RC%"=="0" (
    echo Done: %EXE%
    echo.
    echo Copy it anywhere. It keeps its index and settings in the folder the
    echo exe sits in, so give it a folder of its own.
) else (
    echo The exe was built but the self-test above found problems, so it may
    echo not open properly. Send build-win-log.txt if you want it looked at.
)
echo.
echo Log: %LOG%
pause
exit /b 0

:nopython
echo    ERROR: Python 3.9+ is needed to BUILD the exe ^(not to run it^), and
echo    it could not be installed automatically.
echo    Install it from https://www.python.org/downloads/windows/
echo    ^(tick "Add python.exe to PATH"^), then run this again.
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
