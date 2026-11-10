@echo off
rem Double-click once after downloading: installs everything and creates the
rem "Reelm Encoder" shortcut in this folder. Safe to run again (e.g. after updating).
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not installed. It installs Python and the packages this app needs.
  echo Install it with this command, then open a NEW terminal and run setup.cmd again:
  echo.
  echo     winget install --id=astral-sh.uv -e
  echo.
  pause
  exit /b 1
)

echo Installing the packages this app needs...
uv sync
if errorlevel 1 (
  echo.
  echo Install failed: see the message above.
  pause
  exit /b 1
)

set "LNK=%~dp0Reelm Encoder.lnk"
set "TARGET=%~dp0.venv\Scripts\pythonw.exe"
set "ARGS=-m reelm_encoder.desktop"
set "WD=%~dp0."
set "ICON=%~dp0src\reelm_encoder\assets\icon.ico,0"
powershell -NoProfile -NonInteractive -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath=$env:TARGET; $s.Arguments=$env:ARGS; $s.WorkingDirectory=$env:WD; $s.IconLocation=$env:ICON; $s.Save()"

echo.
echo Checking ffmpeg and the graphics card:
uv run reelm-encoder check
if errorlevel 1 (
  echo.
  echo Reelm Encoder needs ffmpeg. Install it with this command, then you're ready:
  echo.
  echo     winget install Gyan.FFmpeg
)

echo.
echo Done. Double-click "Reelm Encoder" in this folder to start.
pause
