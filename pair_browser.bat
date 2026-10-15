@echo off
rem Pair this computer's default browser with EXO (run once per browser).
cd /d "%~dp0"
C:\Python314\python.exe tools\pair_browser.py %*
if errorlevel 1 pause
