@echo off
rem Desktop entry point: import the inbox, then transcribe + export + summarize the newest recording.
title teamsrec - zpracovat posledni nahravku
call "%~dp0teamsrec-transcribe.cmd" process --latest %*
echo.
if errorlevel 1 (echo CHYBA - viz vypis vyse.) else (echo Hotovo.)
pause
