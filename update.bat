@echo off
rem ImgSortSearch aktualisieren: neuester Stand von GitHub, Daten und Modelle bleiben erhalten.
rem Alles in einer Zeile, weil das Update diese Datei selbst ersetzen kann.
cd /d "%~dp0" & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %* & exit /b
