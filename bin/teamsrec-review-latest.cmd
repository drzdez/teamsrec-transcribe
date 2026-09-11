@echo off
rem Desktop entry point: open the local review page (name the speakers by ear) for the newest recording.
title teamsrec - zkontrolovat mluvci
call "%~dp0teamsrec-transcribe.cmd" review latest %*
if errorlevel 1 (echo CHYBA - viz vypis vyse. & pause)
