@echo off
rem Run the crash monitor once and open the report.
cd /d "%~dp0"
py monitor.py
start "" "%~dp0report.html"
