@echo off
rem findex desktop app - portable: everything it needs lives in this folder.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv-win"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

if exist "%PY%" (
    "%PY%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo Local environment is broken ^(folder moved?^) - rebuilding...
        rmdir /s /q "%VENV%"
    )
)

if not exist "%PY%" (
    echo First run from this location - creating a local environment...
    py -3 -m venv "%VENV%" 2>nul
    if not exist "%PY%" python -m venv "%VENV%"
    if not exist "%PY%" (
        echo ERROR: Could not create a virtual environment.
        echo Install Python 3.9 or later from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip --quiet
    echo Installing PyMuPDF...
    "%PY%" -m pip install pymupdf --quiet
    if errorlevel 1 (
        echo WARNING: PyMuPDF install failed - PDFs will not be indexed.
        pause
    )
    echo Setup complete.
)

"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo ERROR: this Python has no tkinter, so the window cannot open.
    echo Reinstall Python and tick "tcl/tk and IDLE" in the optional features.
    pause
    exit /b 1
)

if exist "%PYW%" (
    start "" "%PYW%" "%HERE%findex_gui.py" %*
) else (
    start "" "%PY%" "%HERE%findex_gui.py" %*
)
endlocal
