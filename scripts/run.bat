@echo off
setlocal
cd /d "%~dp0.."

REM Prefer "py -3" launcher, fallback to python
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found. Running install...
    call "%~dp0install.bat"
    if not exist "venv\Scripts\python.exe" (
        echo [ERROR] Install failed. Install Python 3.10+ from https://www.python.org/downloads/
        echo         Make sure "Add python.exe to PATH" is checked.
        pause
        exit /b 1
    )
)

echo [%date% %time%] Starting Mail Sorter...
echo Open browser: http://localhost:8080
echo.
venv\Scripts\python.exe main.py

echo.
echo [%date% %time%] Process exited with code %ERRORLEVEL%
pause
