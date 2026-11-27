@echo off
rem ImgSortSearch starten
cd /d "%~dp0"
if not exist "%~dp0umgebung\venv\Scripts\pythonw.exe" (
  echo Bitte zuerst install.bat ausfuehren.
  pause
  exit /b 1
)
set "IMGSORTSEARCH_HOME=%~dp0daten"
set "HF_HOME=%~dp0daten\modelle\hf"
set "TORCH_HOME=%~dp0daten\modelle\torch"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0umgebung\exiftool;%PATH%"
start "" "%~dp0umgebung\venv\Scripts\pythonw.exe" -m imgsortsearch %*
