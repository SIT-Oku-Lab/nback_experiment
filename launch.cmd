@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\v1.2.4\NBackExperiment\NBackExperiment.exe" (
    start "" "%~dp0dist\v1.2.4\NBackExperiment\NBackExperiment.exe"
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0app.py"
    if errorlevel 1 pause
    exit /b
)
echo NBackExperiment v1.2.4 or Python 3.10 or later with tkinter is required. See README_ja.md.
pause
