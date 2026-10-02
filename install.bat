@echo off
REM Installs Sonora: private venv + mpv + ffmpeg. Double-click or run from a terminal.
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python not found. Run this, then open a NEW terminal and re-run install.bat:
  echo   winget install Python.Python.3.12
  pause
  exit /b 1
)

py -3 -m venv .venv || (pause & exit /b 1)
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -U -r requirements.txt || (pause & exit /b 1)

echo.
echo Installing mpv and ffmpeg (errors are fine if they are already installed)...
winget install -e --id mpv.mpv --accept-source-agreements --accept-package-agreements
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements

echo.
echo Done. CLOSE this window, open a NEW Windows Terminal, go to this folder and run:  sonora.bat
pause
