@echo off
rem Laedt nur exiftool (neu) in den Ordner "umgebung\exiftool". Python, Pakete und Modelle bleiben.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -NurExiftool
echo.
pause
