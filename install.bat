@echo off
rem ImgSortSearch - Installation (alles bleibt in diesem Ordner)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
