@echo off
setlocal
cd /d "%~dp0.."

echo ========================================
echo  Mail Sorter - Install
echo ========================================
echo.

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

echo Using: %PY%
%PY% --version
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo During setup check: "Add python.exe to PATH"
    pause
    exit /b 1
)

echo.
echo Creating virtual environment...
if exist "venv\Scripts\python.exe" (
    echo venv already exists, skipping create.
) else (
    %PY% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
)

echo.
echo Installing packages from requirements.txt...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Install OK
echo ========================================
echo 1. Run: scripts\run.bat
echo 2. Open: http://localhost:8080
echo 3. First start opens setup wizard in browser
echo.
pause
