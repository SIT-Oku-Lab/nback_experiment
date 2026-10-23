@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\v1.1.0\NBackExperiment\NBackExperiment.exe" (
    start "" "%~dp0dist\v1.1.0\NBackExperiment\NBackExperiment.exe"
    exit /b
)
if exist "%~dp0dist\NBackExperiment\NBackExperiment.exe" (
    start "" "%~dp0dist\NBackExperiment\NBackExperiment.exe"
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0app.py"
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10 or later with tkinter is required. See README_ja.md.
pause
