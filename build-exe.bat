@echo off
rem OPTIONAL: build a single standalone findex.exe that runs without Python.
rem The normal folder + findex-gui.bat setup is unchanged by this.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv-build"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating a build environment...
    py -3 -m venv "%VENV%" 2>nul
    if not exist "%PY%" python -m venv "%VENV%"
)
if not exist "%PY%" (
    echo ERROR: Python 3.9+ is needed to BUILD the exe ^(not to run it^).
    pause
    exit /b 1
)

"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install --only-binary :all: pyinstaller
if errorlevel 1 (
    echo ERROR: could not install PyInstaller.
    pause
    exit /b 1
)
echo Installing components to bake in...
"%PY%" -m pip install --only-binary :all: pymupdf mutagen extract-msg watchdog
if errorlevel 1 echo WARNING: some components missing - exe builds without them.
"%PY%" -m pip install --only-binary :all: winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization winrt-Windows.Graphics.Imaging winrt-Windows.Media.Ocr winrt-Windows.Storage.Streams
if errorlevel 1 echo WARNING: Windows OCR components missing - exe builds without OCR.

set "WINRT="
"%PY%" -c "import winrt" >nul 2>&1 && set "WINRT=--collect-submodules winrt"

echo Building...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name findex %WINRT% --collect-submodules watchdog "%HERE%findex_app.py"
if errorlevel 1 (
    echo Build failed - the messages above say why.
    pause
    exit /b 1
)
echo.
echo Done: dist\findex.exe
echo Copy it anywhere - it creates and keeps its index next to itself.
pause
