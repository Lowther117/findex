@echo off
rem findex CLI - portable: everything it needs lives in this folder.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv-win"
set "PY=%VENV%\Scripts\python.exe"

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
    echo.
)

if "%~1"=="" (
    "%PY%" "%HERE%findex.py" --help
    echo.
    echo Examples:
    echo   findex index D:\ E:\Documents
    echo   findex search "quarterly AND revenue"
    echo   findex search "invoice" -e pdf docx -n 50
    echo   findex name "*budget*"
    echo   findex stats
    echo   findex gui
    echo.
    pause
    exit /b 0
)

"%PY%" "%HERE%findex.py" %*
endlocal
