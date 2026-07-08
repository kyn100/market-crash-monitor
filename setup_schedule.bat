@echo off
rem Registers two Windows scheduled tasks that refresh the crash monitor
rem twice a day (8:00 and 17:00). Re-run this file if you move the folder.
rem Uses pythonw.exe so no console window flashes.

set PYW=C:\Python314\pythonw.exe
if not exist "%PYW%" set PYW=pythonw.exe

schtasks /Create /F /TN "MarketCrashMonitor_Morning" /SC DAILY /ST 08:00 /TR "\"%PYW%\" \"%~dp0monitor.py\""
schtasks /Create /F /TN "MarketCrashMonitor_Evening" /SC DAILY /ST 17:00 /TR "\"%PYW%\" \"%~dp0monitor.py\""

echo.
echo Done. The monitor will refresh report.html daily at 08:00 and 17:00.
echo To change times: Task Scheduler ^> MarketCrashMonitor_Morning / _Evening
pause
