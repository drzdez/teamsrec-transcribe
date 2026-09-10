@echo off
rem Launcher for the source checkout: uses the repo's uv venv and the WinGet ffmpeg.
rem Add this folder to PATH (or call it by full path) and use it like the installed command.
setlocal
set "REPO=%~dp0.."
rem Czech names (speakers, titles) print correctly in the console
chcp 65001 >nul
set "PYTHONUTF8=1"
if "%TEAMSREC_FFMPEG_DIR%"=="" (
  for /d %%p in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
    for /d %%d in ("%%~fp\ffmpeg-*") do set "TEAMSREC_FFMPEG_DIR=%%~fd\bin"
  )
)
"%REPO%\.venv\Scripts\teamsrec-transcribe.exe" %*
endlocal
